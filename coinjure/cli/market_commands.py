"""CLI commands for browsing prediction markets on Polymarket and Kalshi."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import click
import httpx

# ---------------------------------------------------------------------------
# Polymarket helpers (via Gamma API — no auth required)
# ---------------------------------------------------------------------------

GAMMA_EVENTS_URL = 'https://gamma-api.polymarket.com/events'
GAMMA_MARKETS_URL = 'https://gamma-api.polymarket.com/markets'
CLOB_PRICES_HISTORY_URL = 'https://clob.polymarket.com/prices-history'


def _parse_clob_ids(mkt: dict) -> list[str]:
    """Return the parsed list of CLOB token IDs from a market dict (handles JSON-string encoding)."""
    raw = mkt.get('clobTokenIds') or mkt.get('clob_token_ids') or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = []
    return list(raw)


async def _polymarket_list_markets(
    limit: int,
    *,
    tag: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    markets: list[dict[str, Any]] = []
    offset = 0
    page_size = 100  # Gamma API max per request

    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(markets) < limit:
            params: dict[str, Any] = {
                'active': 'true',
                'closed': 'false',
                'limit': page_size,
                'offset': offset,
            }
            if tag:
                params['tag'] = tag
            resp = await client.get(GAMMA_EVENTS_URL, params=params)
            if resp.status_code != 200:
                if offset == 0:
                    raise click.ClickException(
                        f'Polymarket API returned HTTP {resp.status_code}: {resp.text[:200]}'
                    )
                break
            events = resp.json()
            if not events:
                break
            for event in events:
                tags_list = [
                    t.get('label', '') for t in event.get('tags', []) if t.get('label')
                ]
                category = event.get('category', '')
                for mkt in event.get('markets', []):
                    if len(markets) >= limit:
                        break
                    entry: dict[str, Any] = {
                        'id': mkt.get('id', ''),
                        'question': mkt.get('question', ''),
                        'event_id': str(event.get('id', '')),
                        'event_title': event.get('title', ''),
                        'token_id': _parse_clob_ids(mkt)[0]
                        if _parse_clob_ids(mkt)
                        else '',
                        'best_bid': mkt.get('bestBid', ''),
                        'best_ask': mkt.get('bestAsk', ''),
                        'volume': mkt.get('volume', ''),
                        'end_date': mkt.get('endDate', ''),
                        'tags': tags_list,
                        'category': category,
                    }
                    if with_rules:
                        entry['description'] = mkt.get('description', '')
                    markets.append(entry)
                if len(markets) >= limit:
                    break
            offset += len(events)
            if len(events) < page_size:
                break  # No more pages

    return markets[:limit]


async def _polymarket_search_markets(
    query: str,
    limit: int,
    *,
    tag: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    all_markets = await _polymarket_list_markets(500, tag=tag, with_rules=with_rules)
    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('question', '').lower() or q in m.get('event_title', '').lower()
    ]
    return filtered[:limit]


async def _polymarket_market_info(market_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(GAMMA_MARKETS_URL, params={'id': market_id})
    if resp.status_code != 200:
        return None
    data = resp.json()
    if isinstance(data, list) and data:
        mkt = data[0]
    elif isinstance(data, dict):
        mkt = data
    else:
        return None

    clob_ids = _parse_clob_ids(mkt)
    return {
        'id': mkt.get('id', ''),
        'question': mkt.get('question', ''),
        'event_id': str(mkt.get('eventId', '')),
        'token_id': clob_ids[0] if clob_ids else '',
        'no_token_id': clob_ids[1] if len(clob_ids) > 1 else '',
        'best_bid': mkt.get('bestBid', ''),
        'best_ask': mkt.get('bestAsk', ''),
        'volume': mkt.get('volume', ''),
        'end_date': mkt.get('endDate', ''),
        'description': mkt.get('description', ''),
        'active': mkt.get('active', True),
        'closed': mkt.get('closed', False),
    }


# ---------------------------------------------------------------------------
# Kalshi helpers
# ---------------------------------------------------------------------------

KALSHI_API_URL = 'https://api.elections.kalshi.com/trade-api/v2'


async def _kalshi_list_markets(
    limit: int, api_key_id: str | None, private_key_path: str | None
) -> list[dict]:
    from kalshi_python import Configuration
    from kalshi_python.api.markets_api import MarketsApi
    from kalshi_python.api_client import ApiClient

    config = Configuration(host=KALSHI_API_URL)
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')

    api_client = ApiClient(configuration=config)
    if key_id and pk_path:
        api_client.set_kalshi_auth(key_id, pk_path)

    markets_api = MarketsApi(api_client)

    kwargs: dict[str, Any] = {'status': 'open', 'limit': min(limit, 200)}
    response = await asyncio.to_thread(lambda: markets_api.get_markets(**kwargs))
    raw = response.markets if hasattr(response, 'markets') else []
    markets = []
    for m in (raw or [])[:limit]:
        d = m.to_dict() if hasattr(m, 'to_dict') else dict(m)
        markets.append(
            {
                'ticker': d.get('ticker', ''),
                'title': d.get('title', ''),
                'event_ticker': d.get('event_ticker', ''),
                'series_ticker': d.get('series_ticker', ''),
                'yes_bid': d.get('yes_bid', 0),
                'yes_ask': d.get('yes_ask', 0),
                'volume': d.get('volume', 0),
                'close_time': str(d.get('close_time', '')),
                'status': d.get('status', ''),
            }
        )
    return markets


async def _kalshi_search_via_events(
    query: str,
    limit: int,
    with_rules: bool = False,
) -> list[dict]:
    """Search Kalshi events by keyword via raw HTTP and extract nested markets.

    The SDK's EventsApi crashes due to a pydantic validation bug, and the
    SDK's get_markets endpoint is dominated by sports parlays with no
    server-side text search.  Hitting the events endpoint directly with
    httpx (read-only, no auth required) and filtering by title is the
    most reliable approach.
    """
    q = query.lower()
    matched_markets: list[dict] = []
    cursor: str | None = None
    pages = 0
    max_pages = 20  # up to 4 000 events

    while pages < max_pages and len(matched_markets) < limit:
        params: dict[str, Any] = {
            'status': 'open',
            'limit': 200,
            'with_nested_markets': 'true',
        }
        if cursor:
            params['cursor'] = cursor

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f'{KALSHI_API_URL}/events',
                params=params,
            )
        if resp.status_code != 200:
            break

        data = resp.json()
        events = data.get('events', [])
        if not events:
            break

        for event in events:
            title = event.get('title', '').lower()
            if q not in title:
                continue
            for mkt in event.get('markets', []) or []:
                if len(matched_markets) >= limit:
                    break
                entry = {
                    'ticker': mkt.get('ticker', ''),
                    'title': mkt.get('title', ''),
                    'event_ticker': mkt.get('event_ticker', ''),
                    'series_ticker': mkt.get('series_ticker', ''),
                    'yes_bid': mkt.get('yes_bid', 0),
                    'yes_ask': mkt.get('yes_ask', 0),
                    'volume': mkt.get('volume', 0),
                    'close_time': str(mkt.get('close_time', '')),
                    'status': mkt.get('status', ''),
                }
                if with_rules:
                    entry['rules_primary'] = mkt.get('rules_primary', '')
                matched_markets.append(entry)
            if len(matched_markets) >= limit:
                break

        cursor = data.get('cursor')
        pages += 1
        if not cursor:
            break

    return matched_markets[:limit]


async def _kalshi_search_markets(
    query: str,
    limit: int,
    api_key_id: str | None,
    private_key_path: str | None,
    with_rules: bool = False,
) -> list[dict]:
    return await _kalshi_search_via_events(query, limit, with_rules=with_rules)


async def _kalshi_market_info(
    market_ticker: str, api_key_id: str | None, private_key_path: str | None
) -> dict | None:
    from kalshi_python import Configuration
    from kalshi_python.api.markets_api import MarketsApi
    from kalshi_python.api_client import ApiClient

    config = Configuration(host=KALSHI_API_URL)
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')

    api_client = ApiClient(configuration=config)
    if key_id and pk_path:
        api_client.set_kalshi_auth(key_id, pk_path)

    markets_api = MarketsApi(api_client)

    response = await asyncio.to_thread(lambda: markets_api.get_market(market_ticker))
    if not response:
        return None
    m = response.market if hasattr(response, 'market') else response
    d = m.to_dict() if hasattr(m, 'to_dict') else dict(m)  # type: ignore[union-attr, call-overload]
    return {
        'ticker': d.get('ticker', ''),
        'title': d.get('title', ''),
        'event_ticker': d.get('event_ticker', ''),
        'series_ticker': d.get('series_ticker', ''),
        'yes_bid': d.get('yes_bid', 0),
        'yes_ask': d.get('yes_ask', 0),
        'volume': d.get('volume', 0),
        'close_time': str(d.get('close_time', '')),
        'status': d.get('status', ''),
        'rules_primary': d.get('rules_primary', ''),
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def market() -> None:
    """Explore prediction markets on Polymarket and Kalshi."""


# ---------------------------------------------------------------------------
# market analyze (quantitative single-market or pair analysis)
# ---------------------------------------------------------------------------


@market.command('analyze')
@click.option('--market-id', required=True, help='Market ID or ticker.')
@click.option(
    '--compare',
    default=None,
    help='Second market ID for pair analysis (correlation, cointegration, spread stats).',
)
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option(
    '--interval',
    type=click.Choice(['1h', '6h', '1d', 'max']),
    default='max',
    show_default=True,
    help='Price history interval.',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option(
    '--relation-type',
    type=click.Choice([
        'same_event', 'complementary', 'implication', 'exclusivity',
        'temporal', 'semantic', 'conditional',
    ]),
    default=None,
    help='Relation type — determines analysis method (structural vs cointegration).',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_analyze(
    market_id: str,
    compare: str | None,
    exchange: str,
    interval: str,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    relation_type: str | None,
    as_json: bool,
) -> None:
    """Quantitative analysis of a market or pair of markets.

    Single market: returns market info + price series statistics.

    With --compare: runs pair analysis. The method depends on --relation-type:

    \b
      structural (same_event/implication/complementary/exclusivity):
        checks pricing constraint violations (A≤B, A+B≤1)
      cointegration (temporal/semantic/conditional):
        ADF, Engle-Granger, hedge ratio, half-life
      unspecified: defaults to cointegration analysis

    Lead-lag detection runs for all types.
    """
    import math

    # Fetch market info
    try:
        if exchange == 'polymarket':
            info_a = asyncio.run(_polymarket_market_info(market_id))
        else:
            info_a = asyncio.run(
                _kalshi_market_info(
                    market_id, kalshi_api_key_id, kalshi_private_key_path
                )
            )
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch market A: {exc}') from exc
    if info_a is None:
        raise click.ClickException(f'Market not found: {market_id}')

    # Fetch price history for market A
    try:
        if exchange == 'polymarket':
            hist_a = asyncio.run(_polymarket_price_history(market_id, interval, None))
        else:
            hist_a = {'series': []}  # Kalshi has no public price history API
    except Exception:
        hist_a = {'series': []}

    prices_a = [float(p['p']) for p in hist_a.get('series', []) if 'p' in p]
    # Kalshi fallback: use current mid-price as a single data point
    if not prices_a and exchange == 'kalshi':
        bid = info_a.get('yes_bid', 0)
        ask = info_a.get('yes_ask', 0)
        if bid or ask:
            prices_a = [(bid + ask) / 2 / 100]  # Kalshi prices are in cents

    # Single-market stats
    stats_a = _compute_series_stats(prices_a, info_a)

    if compare is None:
        # Single market analysis
        result: dict[str, Any] = {
            'ok': True,
            'market_id': market_id,
            'exchange': exchange,
            'market_info': info_a,
            'series_length': len(prices_a),
            'stats': stats_a,
        }
        if as_json:
            click.echo(json.dumps(result, default=str))
            return
        click.echo(f'\nAnalysis for {exchange} market: {market_id}')
        click.echo(f'  Question: {info_a.get("question", "?")}')
        click.echo(f'  Series points: {len(prices_a)}')
        click.echo('=' * 60)
        for key, val in stats_a.items():
            if isinstance(val, float):
                click.echo(f'  {key}: {val:.6f}')
            else:
                click.echo(f'  {key}: {val}')
        click.echo()
        return

    # ── Pair analysis ─────────────────────────────────────────────────
    if not as_json:
        click.echo(f'Analyzing pair: {market_id} vs {compare} ...')

    try:
        if exchange == 'polymarket':
            info_b = asyncio.run(_polymarket_market_info(compare))
        else:
            info_b = asyncio.run(
                _kalshi_market_info(compare, kalshi_api_key_id, kalshi_private_key_path)
            )
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch market B: {exc}') from exc
    if info_b is None:
        raise click.ClickException(f'Market not found: {compare}')

    try:
        if exchange == 'polymarket':
            hist_b = asyncio.run(_polymarket_price_history(compare, interval, None))
        else:
            hist_b = {'series': []}
    except Exception:
        hist_b = {'series': []}

    prices_b = [float(p['p']) for p in hist_b.get('series', []) if 'p' in p]
    # Kalshi fallback: use current mid-price as a single data point
    if not prices_b and exchange == 'kalshi':
        bid = info_b.get('yes_bid', 0)
        ask = info_b.get('yes_ask', 0)
        if bid or ask:
            prices_b = [(bid + ask) / 2 / 100]
    stats_b = _compute_series_stats(prices_b, info_b)

    # Pair quantitative analysis (method depends on relation type)
    pair_stats = _compute_pair_stats(prices_a, prices_b, relation_type=relation_type)

    result = {
        'ok': True,
        'market_a': {
            'market_id': market_id,
            'info': info_a,
            'series_length': len(prices_a),
            'stats': stats_a,
        },
        'market_b': {
            'market_id': compare,
            'info': info_b,
            'series_length': len(prices_b),
            'stats': stats_b,
        },
        'exchange': exchange,
        'pair_stats': pair_stats,
    }

    if as_json:
        click.echo(json.dumps(result, default=str))
        return

    click.echo(f'\nPair Analysis ({relation_type or "auto"})')
    click.echo('=' * 70)
    q_a = info_a.get('question') or info_a.get('title') or '?'
    q_b = info_b.get('question') or info_b.get('title') or '?'
    bid_a = info_a.get('best_bid') or info_a.get('yes_bid') or '?'
    ask_a = info_a.get('best_ask') or info_a.get('yes_ask') or '?'
    bid_b = info_b.get('best_bid') or info_b.get('yes_bid') or '?'
    ask_b = info_b.get('best_ask') or info_b.get('yes_ask') or '?'
    click.echo(f'  A: {q_a[:65]}')
    click.echo(f'     id={market_id}  bid={bid_a}  ask={ask_a}  pts={len(prices_a)}')
    click.echo(f'  B: {q_b[:65]}')
    click.echo(f'     id={compare}  bid={bid_b}  ask={ask_b}  pts={len(prices_b)}')
    click.echo()

    ps = pair_stats
    at = ps.get('analysis_type', '?')
    click.echo(f'  {"Metric":<22} {"Value":>10}')
    click.echo(f'  {"─"*22} {"─"*10}')
    click.echo(f'  {"Analysis type":<22} {at:>10}')
    click.echo(f'  {"Correlation":<22} {ps.get("correlation", 0):>10.3f}')
    click.echo(f'  {"Aligned points":<22} {ps.get("aligned_points", 0):>10}')
    click.echo(f'  {"Mean spread":<22} {ps.get("mean_spread", 0):>10.4f}')
    click.echo(f'  {"Current spread":<22} {ps.get("current_spread", 0):>10.4f}')
    click.echo(f'  {"Spread zscore":<22} {ps.get("spread_zscore", 0):>10.2f}')

    if at == 'structural':
        click.echo()
        click.echo(f'  {"Constraint":<22} {ps.get("constraint", "?"):>10}')
        holds = ps.get('constraint_holds')
        click.echo(f'  {"Holds":<22} {"YES" if holds else "NO":>10}')
        click.echo(f'  {"Violations":<22} {ps.get("violation_count", 0):>10}')
        click.echo(f'  {"Violation rate":<22} {ps.get("violation_rate", 0)*100:>9.1f}%')
        click.echo(f'  {"Current arb":<22} {ps.get("current_arb", 0):>10.4f}')
        click.echo(f'  {"Mean arb":<22} {ps.get("mean_arb", 0):>10.4f}')
    elif at == 'cointegration':
        click.echo()
        click.echo(f'  {"Cointegrated":<22} {"YES" if ps.get("is_cointegrated") else "NO":>10}')
        click.echo(f'  {"Coint p-value":<22} {ps.get("coint_pvalue", 1):>10.4f}')
        click.echo(f'  {"ADF stationary":<22} {"YES" if ps.get("is_stationary") else "NO":>10}')
        click.echo(f'  {"ADF p-value":<22} {ps.get("adf_pvalue", 1):>10.4f}')
        hl = ps.get('half_life')
        click.echo(f'  {"Half-life (bars)":<22} {hl:>10.1f}' if hl else f'  {"Half-life":<22} {"N/A":>10}')
        click.echo(f'  {"Hedge ratio":<22} {ps.get("hedge_ratio", 0):>10.3f}')

    ll_sig = ps.get('lead_lag_significant')
    ll = ps.get('lead_lag')
    ll_corr = ps.get('lead_lag_corr')
    if ll is not None:
        click.echo()
        click.echo(f'  {"Lead-lag (steps)":<22} {ll:>10}')
        click.echo(f'  {"Lead-lag corr":<22} {ll_corr:>10.3f}' if ll_corr else '')
        click.echo(f'  {"Significant":<22} {"YES" if ll_sig else "NO":>10}')
    click.echo()


def _compute_pair_stats(
    prices_a: list[float],
    prices_b: list[float],
    relation_type: str | None = None,
) -> dict[str, Any]:
    """Compute pair statistics using the appropriate method for the relation type.

    - same_event / implication: structural constraint (A ≤ B)
    - complementary / exclusivity: structural constraint (A + B ≤ 1)
    - temporal / semantic / conditional: cointegration + ADF + half-life
    - None (unspecified): runs cointegration (backward compat)
    - All types also check lead-lag
    """
    n = min(len(prices_a), len(prices_b))
    # Structural types only need 1 point (current snapshot); cointegration needs 5+
    structural_types = {'same_event', 'implication', 'complementary', 'exclusivity'}
    min_points = 1 if relation_type in structural_types else 5
    if n < min_points:
        return {
            'error': 'insufficient_data',
            'points_a': len(prices_a),
            'points_b': len(prices_b),
        }

    a = prices_a[-n:]
    b = prices_b[-n:]

    # Basic stats (always computed, no deps)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
    std_a = (sum((x - mean_a) ** 2 for x in a) / n) ** 0.5
    std_b = (sum((x - mean_b) ** 2 for x in b) / n) ** 0.5
    correlation = cov / (std_a * std_b) if std_a > 0 and std_b > 0 else 0.0

    spread = [a[i] - b[i] for i in range(n)]
    mean_spread = sum(spread) / n
    std_spread = (sum((s - mean_spread) ** 2 for s in spread) / n) ** 0.5

    result: dict[str, Any] = {
        'aligned_points': n,
        'correlation': correlation,
        'mean_spread': mean_spread,
        'std_spread': std_spread,
        'current_spread': spread[-1] if spread else None,
        'spread_zscore': (spread[-1] - mean_spread) / std_spread
        if std_spread > 0
        else 0.0,
    }

    # Type-dispatched validation
    try:
        from coinjure.market.validation import (
            validate_by_type,
            validate_cointegration,
            validate_lead_lag,
        )

        if relation_type:
            vr = validate_by_type(a, b, relation_type)
        else:
            vr = validate_cointegration(a, b)

        # Structural fields
        if vr.analysis_type == 'structural':
            result.update(
                {
                    'analysis_type': 'structural',
                    'constraint': vr.constraint,
                    'constraint_holds': vr.constraint_holds,
                    'violation_count': vr.violation_count,
                    'violation_rate': vr.violation_rate,
                    'current_arb': vr.current_arb,
                    'mean_arb': vr.mean_arb,
                }
            )
        else:
            # Cointegration fields
            result.update(
                {
                    'analysis_type': 'cointegration',
                    'hedge_ratio': vr.hedge_ratio,
                    'adf_statistic': vr.adf_statistic,
                    'adf_pvalue': vr.adf_pvalue,
                    'is_stationary': vr.is_stationary,
                    'coint_statistic': vr.coint_statistic,
                    'coint_pvalue': vr.coint_pvalue,
                    'is_cointegrated': vr.is_cointegrated,
                    'half_life': vr.half_life,
                }
            )

        # Always add lead-lag (supplementary for all types)
        ll = validate_lead_lag(a, b)
        result.update(
            {
                'lead_lag': ll.lead_lag,
                'lead_lag_corr': ll.lead_lag_corr,
                'lead_lag_significant': ll.lead_lag_significant,
            }
        )
    except (ImportError, Exception):
        result['note'] = 'install statsmodels+scipy for advanced analysis'

    return result


def _compute_series_stats(prices: list[float], info: dict) -> dict[str, Any]:
    """Compute single-series statistics (no external deps beyond stdlib)."""
    if not prices:
        return {'error': 'no_price_data'}

    n = len(prices)
    mean_price = sum(prices) / n
    variance = sum((p - mean_price) ** 2 for p in prices) / n if n > 1 else 0.0
    std_price = variance**0.5

    returns = [prices[i] - prices[i - 1] for i in range(1, n)]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    vol = (
        (sum((r - mean_return) ** 2 for r in returns) / len(returns)) ** 0.5
        if returns
        else 0.0
    )

    bid_raw = info.get('best_bid')
    ask_raw = info.get('best_ask')
    try:
        bid = float(bid_raw) if bid_raw not in (None, '') else None
    except (ValueError, TypeError):
        bid = None
    try:
        ask = float(ask_raw) if ask_raw not in (None, '') else None
    except (ValueError, TypeError):
        ask = None
    ba_spread = ask - bid if bid is not None and ask is not None else None

    trend = prices[-1] - prices[0] if n >= 2 else 0.0

    return {
        'mean_price': mean_price,
        'std_price': std_price,
        'volatility': vol,
        'min_price': min(prices),
        'max_price': max(prices),
        'last_price': prices[-1],
        'trend': trend,
        'bid_ask_spread': ba_spread,
    }


# ---------------------------------------------------------------------------
# market relations (manage the persisted relation graph)
# ---------------------------------------------------------------------------


@market.group('relations')
def market_relations_group() -> None:
    """Manage the persisted market relation graph."""


@market_relations_group.command('add')
@click.option('--market-id-a', required=True, help='Market ID for leg A.')
@click.option('--market-id-b', required=True, help='Market ID for leg B.')
@click.option(
    '--exchange',
    'exc',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option(
    '--spread-type',
    default='semantic',
    show_default=True,
    help='Relation type (semantic, same_event, implication).',
)
@click.option('--hypothesis', default='', help='Price relationship hypothesis.')
@click.option('--reasoning', default='', help='Why these markets are related.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_add(
    market_id_a: str,
    market_id_b: str,
    exc: str,
    spread_type: str,
    hypothesis: str,
    reasoning: str,
    as_json: bool,
) -> None:
    """Create a relation between two markets.

    Fetches market info from the API and persists the relation.
    Use ``market analyze --compare`` first to verify correlation.

    \b
      coinjure market analyze --market-id 610380 --compare 610381 --json
      coinjure market relations add \\
        --market-id-a 610380 --market-id-b 610381 \\
        --spread-type semantic \\
        --hypothesis "p_A ~ p_B (positive)" \\
        --reasoning "election called is prerequisite for held" --json
    """
    from coinjure.market.relations import MarketRelation, RelationStore

    async def _fetch_info(mid: str) -> dict:
        if exc == 'polymarket':
            info = await _polymarket_market_info(mid)
        else:
            info = await _kalshi_market_info(
                mid,
                api_key_id=None,
                private_key_path=None,
            )
        if info is None:
            raise click.ClickException(f'Market not found: {mid}')
        info['platform'] = exc
        return info

    try:
        info_a = asyncio.run(_fetch_info(market_id_a))
        info_b = asyncio.run(_fetch_info(market_id_b))
    except Exception as exc_err:
        raise click.ClickException(
            f'Failed to fetch market info: {exc_err}'
        ) from exc_err

    rid = f'{market_id_a[:8]}-{market_id_b[:8]}'

    rel = MarketRelation(
        relation_id=rid,
        market_a=info_a,
        market_b=info_b,
        spread_type=spread_type,
        reasoning=reasoning,
        hypothesis=hypothesis,
    )

    store = RelationStore()
    store.add(rel)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'relation_id': rid,
                    'relation': rel.to_dict(),
                },
                default=str,
            )
        )
        return

    click.echo(f'\nRelation created: {rid}')
    click.echo(f'  A: [{info_a.get("platform")}] {info_a.get("question", "")[:60]}')
    click.echo(f'  B: [{info_b.get("platform")}] {info_b.get("question", "")[:60]}')
    click.echo(f'  type={spread_type} hypothesis={hypothesis}')
    click.echo()


@market_relations_group.command('list')
@click.option('--type', 'spread_type', default=None, help='Filter by spread type.')
@click.option(
    '--status',
    default=None,
    help='Filter by status (active, deployed, invalidated, retired).',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_list(
    spread_type: str | None,
    status: str | None,
    as_json: bool,
) -> None:
    """List all stored market relations."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    relations = store.list(spread_type=spread_type, status=status)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'count': len(relations),
                    'relations': [r.to_dict() for r in relations],
                },
                default=str,
            )
        )
        return

    if not relations:
        click.echo('No relations found.')
        return

    click.echo(f'\n{len(relations)} market relation(s):\n')
    for r in relations:
        ma = r.market_a
        mb = r.market_b
        click.echo(
            f'  [{r.relation_id}] {r.spread_type}  conf={r.confidence:.2f}  '
            f'status={r.status}'
        )
        click.echo(f'    A: [{ma.get("platform", "?")}] {ma.get("question", "?")[:60]}')
        click.echo(f'    B: [{mb.get("platform", "?")}] {mb.get("question", "?")[:60]}')
        if r.hypothesis:
            click.echo(f'    Hypothesis: {r.hypothesis}')
        click.echo()


@market_relations_group.command('remove')
@click.argument('relation_id')
def relations_remove(relation_id: str) -> None:
    """Remove a relation from the store."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    if store.remove(relation_id):
        click.echo(f'Removed relation: {relation_id}')
    else:
        raise click.ClickException(f'Relation not found: {relation_id}')


@market_relations_group.command('validate')
@click.argument('relation_id')
@click.option(
    '--interval',
    type=click.Choice(['1h', '6h', '1d', 'max']),
    default='max',
    show_default=True,
    help='Price history interval.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_validate(relation_id: str, interval: str, as_json: bool) -> None:
    """Validate a relation with type-specific quantitative analysis.

    Fetches price history for both legs and runs the appropriate validation
    based on the relation's spread_type.
    """
    from coinjure.market.relations import RelationStore, ValidationResult

    store = RelationStore()
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    platform_a = rel.market_a.get('platform', 'polymarket')
    platform_b = rel.market_b.get('platform', 'polymarket')
    mid_a = rel.market_a.get('id', '')
    mid_b = rel.market_b.get('id', '')

    if not mid_a or not mid_b:
        raise click.ClickException('Relation is missing market IDs')

    # Fetch price history
    try:
        if platform_a == 'polymarket':
            hist_a = asyncio.run(_polymarket_price_history(mid_a, interval, None))
        else:
            hist_a = {'series': []}
        if platform_b == 'polymarket':
            hist_b = asyncio.run(_polymarket_price_history(mid_b, interval, None))
        else:
            hist_b = {'series': []}
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch price history: {exc}') from exc

    prices_a = [float(p['p']) for p in hist_a.get('series', []) if 'p' in p]
    prices_b = [float(p['p']) for p in hist_b.get('series', []) if 'p' in p]

    if len(prices_a) < 5 or len(prices_b) < 5:
        raise click.ClickException(
            f'Insufficient price data: A={len(prices_a)}, B={len(prices_b)}'
        )

    # Run type-dispatched analysis
    pair_stats = _compute_pair_stats(prices_a, prices_b, relation_type=rel.spread_type)

    # Build ValidationResult from pair_stats
    vr = ValidationResult(
        analysis_type=pair_stats.get('analysis_type'),
        constraint=pair_stats.get('constraint'),
        constraint_holds=pair_stats.get('constraint_holds'),
        violation_count=pair_stats.get('violation_count'),
        violation_rate=pair_stats.get('violation_rate'),
        current_arb=pair_stats.get('current_arb'),
        mean_arb=pair_stats.get('mean_arb'),
        adf_statistic=pair_stats.get('adf_statistic'),
        adf_pvalue=pair_stats.get('adf_pvalue'),
        is_stationary=pair_stats.get('is_stationary'),
        coint_statistic=pair_stats.get('coint_statistic'),
        coint_pvalue=pair_stats.get('coint_pvalue'),
        is_cointegrated=pair_stats.get('is_cointegrated'),
        half_life=pair_stats.get('half_life'),
        hedge_ratio=pair_stats.get('hedge_ratio'),
        correlation=pair_stats.get('correlation'),
        mean_spread=pair_stats.get('mean_spread'),
        std_spread=pair_stats.get('std_spread'),
        lead_lag=pair_stats.get('lead_lag'),
        lead_lag_corr=pair_stats.get('lead_lag_corr'),
        lead_lag_significant=pair_stats.get('lead_lag_significant'),
    )

    rel.set_validation(vr)
    store.update(rel)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'relation_id': relation_id,
                    'status': rel.status,
                    'validation': rel.validation,
                },
                default=str,
            )
        )
        return

    click.echo(f'\nValidated relation: {relation_id}')
    click.echo(f'  Status: {rel.status}')
    click.echo(f'  Analysis type: {vr.analysis_type}')
    if vr.analysis_type == 'structural':
        click.echo(f'  Constraint: {vr.constraint}')
        click.echo(f'  Holds: {vr.constraint_holds}')
        click.echo(f'  Violations: {vr.violation_count} ({(vr.violation_rate or 0)*100:.1f}%)')
    else:
        click.echo(f'  Cointegrated: {vr.is_cointegrated}')
        click.echo(f'  ADF p-value: {vr.adf_pvalue}')
        click.echo(f'  Half-life: {vr.half_life}')
    if vr.lead_lag_significant:
        click.echo(f'  Lead-lag: {vr.lead_lag} (corr={vr.lead_lag_corr:.3f})')
    click.echo()


# ---------------------------------------------------------------------------
# market discover (multi-keyword search + structural pair detection)
# ---------------------------------------------------------------------------


@market.command('discover')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'both']),
    default='both',
    show_default=True,
)
@click.option(
    '--query',
    '-q',
    multiple=True,
    help='Search keywords (repeatable). Searches each keyword on each exchange '
    'and merges unique results. If omitted, fetches top markets by volume.',
)
@click.option(
    '--tag',
    '-t',
    multiple=True,
    help='Polymarket event tags to filter by (repeatable). '
    'e.g. -t Crypto -t Finance. Merged with --query results.',
)
@click.option(
    '--limit',
    default=100,
    show_default=True,
    type=int,
    help='Markets to fetch per exchange (or per query when --query is used).',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option(
    '--with-rules/--no-rules',
    'with_rules',
    default=True,
    show_default=True,
    help='Include resolution rules/description. Use --no-rules to omit.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_discover(
    exchange: str,
    query: tuple[str, ...],
    tag: tuple[str, ...],
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    with_rules: bool,
    as_json: bool,
) -> None:
    """Fetch markets from exchanges for agent analysis.

    Searches multiple keywords and/or tags, merges unique markets, and returns
    the raw data. The agent decides which markets are related and adds them
    to the relation store via ``market relations add``.

    Use --with-rules to include resolution criteria (for cross-platform
    same_event comparison).

    \b
      coinjure market discover -q "election" -q "Trump" --json
      coinjure market discover -q "Trump resign" --exchange both --with-rules --json
      coinjure market discover -t Crypto -t Finance --exchange polymarket --json
    """

    # ── Fetch markets ──────────────────────────────────────────────────

    async def _fetch_markets() -> tuple[list[dict], list[dict]]:
        poly_markets: list[dict] = []
        kalshi_markets: list[dict] = []
        poly_seen: set[str] = set()
        kalshi_seen: set[str] = set()

        def _merge_poly(results: list[dict]) -> None:
            for mk in results:
                mid = mk.get('id', '')
                if mid and mid not in poly_seen:
                    poly_seen.add(mid)
                    poly_markets.append(mk)

        def _merge_kalshi(results: list[dict]) -> None:
            for mk in results:
                mid = mk.get('ticker', '')
                if mid and mid not in kalshi_seen:
                    kalshi_seen.add(mid)
                    kalshi_markets.append(mk)

        has_filters = query or tag

        if has_filters:
            for q in query:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(await _polymarket_search_markets(
                        q, limit, with_rules=with_rules,
                    ))
                if exchange in ('kalshi', 'both'):
                    _merge_kalshi(
                        await _kalshi_search_markets(
                            q,
                            limit,
                            kalshi_api_key_id,
                            kalshi_private_key_path,
                            with_rules=with_rules,
                        )
                    )
            for t in tag:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(await _polymarket_list_markets(
                        limit, tag=t, with_rules=with_rules,
                    ))
        else:
            if exchange in ('polymarket', 'both'):
                poly_markets = await _polymarket_list_markets(
                    limit, with_rules=with_rules,
                )
            if exchange in ('kalshi', 'both'):
                kalshi_markets = await _kalshi_list_markets(
                    limit,
                    kalshi_api_key_id,
                    kalshi_private_key_path,
                )

        return poly_markets, kalshi_markets

    try:
        poly_markets, kalshi_markets = asyncio.run(_fetch_markets())
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch markets: {exc}') from exc

    if not poly_markets and not kalshi_markets:
        raise click.ClickException('No markets fetched. Check API keys or queries.')

    # ── Annotate with existing relations ──────────────────────────────

    from coinjure.market.relations import RelationStore

    store = RelationStore()
    all_relations = store.list()

    # Build a set of market IDs already in relations
    related_ids: dict[str, list[str]] = {}  # market_id → [relation_id, ...]
    for r in all_relations:
        for mkt in (r.market_a, r.market_b):
            for key in ('id', 'market_id', 'ticker'):
                mid = mkt.get(key, '')
                if mid:
                    related_ids.setdefault(mid, []).append(r.relation_id)

    # Tag each market with its existing relations
    for mk in poly_markets:
        mid = mk.get('id', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]
    for mk in kalshi_markets:
        mid = mk.get('ticker', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]

    # Compact summary of existing relations for agent context
    relation_summary = [
        {
            'relation_id': r.relation_id,
            'type': r.spread_type,
            'status': r.status,
            'market_a_id': r.market_a.get('id', r.market_a.get('ticker', '')),
            'market_b_id': r.market_b.get('id', r.market_b.get('ticker', '')),
            'market_a_question': r.market_a.get('question', '')[:60],
            'market_b_question': r.market_b.get('question', '')[:60],
        }
        for r in all_relations
    ]

    # ── Output ─────────────────────────────────────────────────────────

    total = len(poly_markets) + len(kalshi_markets)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'markets': {
                        'polymarket': poly_markets,
                        'kalshi': kalshi_markets,
                    },
                    'total_markets': total,
                    'existing_relations': relation_summary,
                    'existing_relation_count': len(relation_summary),
                },
                default=str,
            )
        )
        return

    parts = [f'"{q}"' for q in query] + [f'tag:{t}' for t in tag]
    query_desc = ', '.join(parts) if parts else 'top by volume'
    click.echo(
        f'\nDiscovered {len(poly_markets)} Polymarket + {len(kalshi_markets)} Kalshi '
        f'markets (queries: {query_desc})\n'
    )

    def _fmt_table(
        markets: list[dict], platform: str,
    ) -> None:
        if not markets:
            return
        click.echo(f'  [{platform}] {len(markets)} markets')
        click.echo(f'  {"ID":>12}  {"Bid":>6}  {"Ask":>6}  {"Volume":>12}  Question')
        click.echo(f'  {"─"*12}  {"─"*6}  {"─"*6}  {"─"*12}  {"─"*50}')
        for m in markets:
            mid = m.get('id') or m.get('ticker', '')
            bid = m.get('best_bid') or m.get('yes_bid', '')
            ask = m.get('best_ask') or m.get('yes_ask', '')
            vol = m.get('volume', 0)
            q = m.get('question') or m.get('title', '')
            try:
                bid_s = f'{float(bid):6.3f}' if bid not in (None, '') else '     —'
            except (ValueError, TypeError):
                bid_s = '     —'
            try:
                ask_s = f'{float(ask):6.3f}' if ask not in (None, '') else '     —'
            except (ValueError, TypeError):
                ask_s = '     —'
            try:
                vol_s = f'{float(str(vol).replace(",", "")):12.0f}'
            except (ValueError, TypeError):
                vol_s = '           0'
            click.echo(f'  {mid:>12}  {bid_s}  {ask_s}  {vol_s}  {q[:55]}')
            if with_rules:
                desc = m.get('description') or m.get('rules_primary', '')
                if desc:
                    # Show first 120 chars of rules
                    click.echo(f'  {"":>12}  Rules: {desc[:120]}...')
        click.echo()

    _fmt_table(poly_markets, 'Polymarket')
    _fmt_table(kalshi_markets, 'Kalshi')


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Polymarket price history
# ---------------------------------------------------------------------------

_INTERVAL_FIDELITY: dict[str, int] = {
    'max': 1,
    '1d': 1440,
    '6h': 360,
    '1h': 60,
}


async def _polymarket_price_history(
    market_id: str, interval: str, limit: int | None
) -> dict:
    fidelity = _INTERVAL_FIDELITY.get(interval, 1440)

    # Resolve the CLOB token ID from the numeric market ID.
    info = await _polymarket_market_info(market_id)
    if info is None:
        raise click.ClickException(f'Market not found: {market_id}')
    token_id = info.get('token_id', '')
    if not token_id:
        raise click.ClickException(f'No CLOB token ID for market: {market_id}')

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            CLOB_PRICES_HISTORY_URL,
            params={'market': token_id, 'interval': interval, 'fidelity': fidelity},
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket CLOB API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    data = resp.json()

    # Response is {"history": [{"t": ..., "p": ...}, ...]}
    raw_history = data.get('history') if isinstance(data, dict) else data
    points: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if isinstance(item, dict) and 't' in item and 'p' in item:
                points.append({'t': item['t'], 'p': item['p']})

    if limit and limit > 0:
        points = points[-limit:]

    first_price = points[0]['p'] if points else None
    last_price = points[-1]['p'] if points else None
    total_move: Any = None
    if first_price is not None and last_price is not None:
        try:
            total_move = round(float(last_price) - float(first_price), 6)
        except (TypeError, ValueError):
            total_move = None

    return {
        'market_id': market_id,
        'token_id': token_id,
        'interval': interval,
        'points': len(points),
        'series': points,
        'first_price': first_price,
        'last_price': last_price,
        'total_move': total_move,
    }


# ---------------------------------------------------------------------------
# market news (absorbed from news_commands.py)
# ---------------------------------------------------------------------------


@market.command('news')
@click.option(
    '--source',
    type=click.Choice(['google', 'rss', 'thenewsapi']),
    default='google',
    show_default=True,
    help='News source to fetch from.',
)
@click.option('--query', default=None, help='Optional search/filter query.')
@click.option(
    '--limit', default=10, show_default=True, type=int, help='Max articles to fetch.'
)
@click.option(
    '--api-token',
    default=None,
    help='TheNewsAPI token (or THENEWSAPI_TOKEN env var). Required for --source thenewsapi.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Output as JSON.')
def market_news(
    source: str, query: str | None, limit: int, api_token: str | None, as_json: bool
) -> None:
    """Fetch news headlines from a specified source."""
    from coinjure.cli.news_commands import (
        _fetch_google_news,
        _fetch_rss,
        _fetch_thenewsapi,
        _format_article,
    )

    token = api_token or os.environ.get('THENEWSAPI_TOKEN', '')

    try:
        if source == 'google':
            articles = asyncio.run(_fetch_google_news(query, limit))
        elif source == 'rss':
            articles = asyncio.run(_fetch_rss(query, limit))
        else:
            if not token:
                raise click.ClickException(
                    'TheNewsAPI requires a token. Pass --api-token or set THENEWSAPI_TOKEN.'
                )
            articles = asyncio.run(_fetch_thenewsapi(query, limit, token))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch news: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps({'source': source, 'count': len(articles), 'articles': articles})
        )
        return

    if not articles:
        click.echo('No articles found.')
        return

    click.echo(f'Fetched {len(articles)} article(s) from {source}:\n')
    for i, article in enumerate(articles, 1):
        click.echo(f'[{i}]')
        click.echo(_format_article(article))
        click.echo()
