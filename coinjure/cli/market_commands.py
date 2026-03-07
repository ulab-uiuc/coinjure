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
) -> list[dict]:
    params: dict[str, Any] = {
        'active': 'true',
        'closed': 'false',
        'limit': min(limit, 100),
    }
    if tag:
        params['tag'] = tag
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(GAMMA_EVENTS_URL, params=params)
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    events = resp.json()
    markets: list[dict[str, Any]] = []
    for event in events[:limit]:
        tags = [t.get('label', '') for t in event.get('tags', []) if t.get('label')]
        category = event.get('category', '')
        for mkt in event.get('markets', []):
            if len(markets) >= limit:
                break
            markets.append(
                {
                    'id': mkt.get('id', ''),
                    'question': mkt.get('question', ''),
                    'event_id': str(event.get('id', '')),
                    'event_title': event.get('title', ''),
                    'token_id': _parse_clob_ids(mkt)[0] if _parse_clob_ids(mkt) else '',
                    'best_bid': mkt.get('bestBid', ''),
                    'best_ask': mkt.get('bestAsk', ''),
                    'volume': mkt.get('volume', ''),
                    'end_date': mkt.get('endDate', ''),
                    'tags': tags,
                    'category': category,
                }
            )
        if len(markets) >= limit:
            break
    return markets[:limit]


async def _polymarket_search_markets(
    query: str,
    limit: int,
    *,
    tag: str | None = None,
) -> list[dict]:
    all_markets = await _polymarket_list_markets(500, tag=tag)
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
                matched_markets.append(
                    {
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
                )
            if len(matched_markets) >= limit:
                break

        cursor = data.get('cursor')
        pages += 1
        if not cursor:
            break

    return matched_markets[:limit]


async def _kalshi_search_markets(
    query: str, limit: int, api_key_id: str | None, private_key_path: str | None
) -> list[dict]:
    return await _kalshi_search_via_events(query, limit)


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
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_analyze(
    market_id: str,
    compare: str | None,
    exchange: str,
    interval: str,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Quantitative analysis of a market or pair of markets.

    Single market: returns market info + price series statistics (volatility,
    trend, bid-ask spread).

    With --compare: returns pair statistics — correlation, cointegration (ADF,
    Engle-Granger), hedge ratio, half-life, spread mean/std.
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
            hist_a = {'series': []}  # Kalshi price history not yet supported
    except Exception:
        hist_a = {'series': []}

    prices_a = [float(p['p']) for p in hist_a.get('series', []) if 'p' in p]

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
    stats_b = _compute_series_stats(prices_b, info_b)

    # Pair quantitative analysis
    pair_stats = _compute_pair_stats(prices_a, prices_b)

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

    click.echo(f'\nPair Analysis: {market_id} vs {compare}')
    click.echo('=' * 60)
    click.echo(f'  A: {info_a.get("question", "?")[:70]}')
    click.echo(f'     points={len(prices_a)}')
    click.echo(f'  B: {info_b.get("question", "?")[:70]}')
    click.echo(f'     points={len(prices_b)}')
    click.echo()
    for key, val in pair_stats.items():
        if isinstance(val, float):
            if math.isnan(val):
                click.echo(f'  {key}: NaN')
            else:
                click.echo(f'  {key}: {val:.6f}')
        else:
            click.echo(f'  {key}: {val}')
    click.echo()


def _compute_series_stats(prices: list[float], info: dict) -> dict[str, Any]:
    """Compute single-series statistics (no external deps beyond stdlib)."""
    if not prices:
        return {'error': 'no_price_data'}

    n = len(prices)
    mean_price = sum(prices) / n
    variance = sum((p - mean_price) ** 2 for p in prices) / n if n > 1 else 0.0
    std_price = variance**0.5

    # Returns (log-returns proxy via differences)
    returns = [prices[i] - prices[i - 1] for i in range(1, n)]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    vol = (
        (sum((r - mean_return) ** 2 for r in returns) / len(returns)) ** 0.5
        if returns
        else 0.0
    )

    # Bid-ask spread
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

    # Trend: first vs last price
    trend = prices[-1] - prices[0] if n >= 2 else 0.0

    result: dict[str, Any] = {
        'mean_price': mean_price,
        'std_price': std_price,
        'volatility': vol,
        'min_price': min(prices),
        'max_price': max(prices),
        'last_price': prices[-1],
        'trend': trend,
        'bid_ask_spread': ba_spread,
    }
    return result


def _compute_pair_stats(prices_a: list[float], prices_b: list[float]) -> dict[str, Any]:
    """Compute pair statistics: correlation, cointegration, spread, half-life."""
    import math

    n = min(len(prices_a), len(prices_b))
    if n < 5:
        return {
            'error': 'insufficient_data',
            'points_a': len(prices_a),
            'points_b': len(prices_b),
        }

    a = prices_a[-n:]
    b = prices_b[-n:]

    # Simple correlation (stdlib, no numpy needed)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
    std_a = (sum((x - mean_a) ** 2 for x in a) / n) ** 0.5
    std_b = (sum((x - mean_b) ** 2 for x in b) / n) ** 0.5
    correlation = cov / (std_a * std_b) if std_a > 0 and std_b > 0 else 0.0

    # Spread stats (simple, hedge_ratio=1)
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

    # Try advanced stats if statsmodels available
    try:
        from coinjure.market.validation import validate_relation

        vr = validate_relation(a, b)
        result.update(
            {
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
    except (ImportError, Exception):
        result['note'] = 'install statsmodels+scipy for cointegration/ADF tests'

    return result


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


@market_relations_group.command('show')
@click.argument('relation_id')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_show(relation_id: str, as_json: bool) -> None:
    """Show details of a specific relation."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    if as_json:
        click.echo(json.dumps({'ok': True, 'relation': rel.to_dict()}, default=str))
        return

    d = rel.to_dict()
    click.echo(f'\nRelation: {rel.relation_id}')
    click.echo('=' * 60)
    for key, val in d.items():
        if key in ('market_a', 'market_b', 'analysis_a', 'analysis_b'):
            click.echo(f'  {key}:')
            if isinstance(val, dict):
                for k2, v2 in val.items():
                    click.echo(f'    {k2}: {v2}')
        else:
            click.echo(f'  {key}: {val}')
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
    '--history-a',
    required=True,
    help='JSONL history file for market A.',
)
@click.option(
    '--history-b',
    required=True,
    help='JSONL history file for market B.',
)
@click.option('--significance', default=0.05, help='Statistical significance level.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_validate(
    relation_id: str,
    history_a: str,
    history_b: str,
    significance: float,
    as_json: bool,
) -> None:
    """Quantitatively validate a relation (cointegration, ADF, half-life)."""
    from coinjure.market.relations import RelationStore
    from coinjure.market.validation import validate_relation

    store = RelationStore()
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    # Load price series from history files
    prices_a = _load_price_series(history_a)
    prices_b = _load_price_series(history_b)

    if len(prices_a) < 30 or len(prices_b) < 30:
        raise click.ClickException(
            f'Insufficient data: A has {len(prices_a)}, B has {len(prices_b)} points (need ≥30)'
        )

    result = validate_relation(prices_a, prices_b, significance=significance)
    rel.set_validation(result)
    store.update(rel)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'relation_id': relation_id,
                    'validation': rel.validation,
                    'status': rel.status,
                },
                default=str,
            )
        )
        return

    click.echo(f'\nValidation for relation {relation_id}:')
    click.echo(f'  Status: {rel.status}')
    click.echo(
        f'  ADF: stat={result.adf_statistic:.4f} p={result.adf_pvalue:.4f} stationary={result.is_stationary}'
    )
    if result.coint_pvalue is not None:
        click.echo(
            f'  Cointegration: stat={result.coint_statistic:.4f} p={result.coint_pvalue:.4f} coint={result.is_cointegrated}'
        )
    if result.half_life is not None:
        click.echo(f'  Half-life: {result.half_life:.1f} bars')
    click.echo(f'  Hedge ratio: {result.hedge_ratio:.4f}')
    click.echo(f'  Correlation: {result.correlation:.4f}')
    click.echo(f'  Spread: mean={result.mean_spread:.4f} std={result.std_spread:.4f}')
    click.echo()


def _load_price_series(history_file: str) -> list[float]:
    """Load a price series from a JSONL history file."""
    import pathlib

    path = pathlib.Path(history_file)
    if not path.exists():
        raise click.ClickException(f'File not found: {history_file}')

    prices: list[float] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            ts = data.get('time_series', {})
            for outcome, points in ts.items():
                for pt in points:
                    p = pt.get('p')
                    if p is not None:
                        prices.append(float(p))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return prices


@market_relations_group.command('find')
@click.argument('market_id')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_find(market_id: str, as_json: bool) -> None:
    """Find all relations involving a specific market."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    relations = store.find_by_market(market_id)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'market_id': market_id,
                    'count': len(relations),
                    'relations': [r.to_dict() for r in relations],
                },
                default=str,
            )
        )
        return

    if not relations:
        click.echo(f'No relations found for market: {market_id}')
        return

    click.echo(f'\n{len(relations)} relation(s) for market {market_id}:\n')
    for r in relations:
        click.echo(
            f'  [{r.relation_id}] {r.spread_type} conf={r.confidence:.2f} status={r.status}'
        )
        click.echo(f'    A: {r.market_a.get("question", "?")[:50]}')
        click.echo(f'    B: {r.market_b.get("question", "?")[:50]}')
        click.echo()


@market_relations_group.command('strongest')
@click.option('-n', default=10, help='Number of top relations to show.')
@click.option('--status', default=None, help='Filter by status.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_strongest(n: int, status: str | None, as_json: bool) -> None:
    """Show the N highest-confidence relations."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    relations = store.strongest(n=n, status=status)

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

    click.echo(f'\nTop {len(relations)} relations by confidence:\n')
    for i, r in enumerate(relations, 1):
        click.echo(
            f'  {i}. [{r.relation_id}] {r.spread_type} conf={r.confidence:.2f} '
            f'status={r.status}'
        )
        click.echo(f'     {r.market_a.get("question", "?")[:45]}')
        click.echo(f'     ↔ {r.market_b.get("question", "?")[:45]}')
    click.echo()


# ---------------------------------------------------------------------------
# market discover (multi-keyword search + structural pair detection)
# ---------------------------------------------------------------------------


def _discover_rules(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
) -> list[dict]:
    """Find spread pairs using deterministic rules (no LLM needed).

    Detects:
    1. temporal/implication — same event, different deadlines: p_early ≤ p_late
    2. complementary — markets in same event whose YES prices should sum to ~1
    3. same_event cross-platform — same question on Poly and Kalshi
    """
    from collections import defaultdict
    from difflib import SequenceMatcher

    pairs: list[dict] = []

    # --- 1. Temporal implication within same event_id (Polymarket) ---
    by_event: dict[str, list[dict]] = defaultdict(list)
    for m in poly_markets:
        eid = m.get('event_id', '')
        if eid:
            by_event[eid].append(m)

    for eid, mkts in by_event.items():
        # Only consider events with priced markets and distinct end_dates
        priced = [m for m in mkts if _safe_price(m) is not None]
        if len(priced) < 2:
            continue

        # Sort by end_date; skip events where markets don't have distinct dates
        # (those are likely mutually-exclusive outcomes, not temporal series)
        dated = [(m, m.get('end_date', '')) for m in priced if m.get('end_date')]
        dated.sort(key=lambda x: x[1])

        # Deduplicate by end_date — if many markets share the same date,
        # they're parallel outcomes (not temporal), skip the event
        unique_dates = {d for _, d in dated}
        if len(unique_dates) < 2:
            continue

        # Only pair ADJACENT deadlines (O(n) not O(n²))
        for i in range(len(dated) - 1):
            m_early, date_early = dated[i]
            m_late, date_late = dated[i + 1]
            if date_early == date_late:
                continue  # same deadline = not a temporal pair
            p_early = _safe_price(m_early)
            p_late = _safe_price(m_late)
            if p_early is None or p_late is None:
                continue
            violation = p_early - p_late
            pairs.append(
                {
                    'market_a': {
                        'id': m_early['id'],
                        'platform': 'polymarket',
                        'question': m_early['question'],
                        'price': p_early,
                        'end_date': date_early,
                    },
                    'market_b': {
                        'id': m_late['id'],
                        'platform': 'polymarket',
                        'question': m_late['question'],
                        'price': p_late,
                        'end_date': date_late,
                    },
                    'spread_type': 'implication',
                    'confidence': 0.95,
                    'reasoning': (
                        f'Same event ({m_early.get("event_title", eid)[:50]}), '
                        f'earlier deadline must have lower probability. '
                        f'Violation={violation:.4f}'
                    ),
                    'hypothesis': 'p_early <= p_late',
                    'method': 'rules',
                    '_violation': violation,
                }
            )

    # --- 2. Cross-platform same-event (fuzzy title match) ---
    for pm in poly_markets:
        p_price = _safe_price(pm)
        if p_price is None:
            continue
        pq = pm.get('question', '').lower()
        for km in kalshi_markets:
            k_price = _safe_price_kalshi(km)
            if k_price is None:
                continue
            kq = km.get('title', '').lower()
            sim = SequenceMatcher(None, pq, kq).ratio()
            if sim >= 0.7:
                spread = abs(p_price - k_price)
                pairs.append(
                    {
                        'market_a': {
                            'id': pm['id'],
                            'platform': 'polymarket',
                            'question': pm['question'],
                            'price': p_price,
                        },
                        'market_b': {
                            'id': km.get('ticker', ''),
                            'platform': 'kalshi',
                            'question': km.get('title', ''),
                            'price': k_price,
                        },
                        'spread_type': 'same_event',
                        'confidence': round(sim, 3),
                        'reasoning': (
                            f'Same event across platforms (similarity={sim:.2f}). '
                            f'Spread={spread:.4f}'
                        ),
                        'hypothesis': 'p_A - p_B ≈ 0',
                        'method': 'rules',
                    }
                )

    return pairs


def _safe_price(m: dict) -> float | None:
    """Extract a usable price from a Polymarket market dict."""
    for key in ('best_ask', 'best_bid'):
        v = m.get(key)
        if v is not None and v != '':
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return None


def _safe_price_kalshi(m: dict) -> float | None:
    """Extract a usable price from a Kalshi market dict."""
    for key in ('yes_ask', 'yes_bid'):
        v = m.get(key)
        if v is not None and v != '':
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return None


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
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_discover(
    exchange: str,
    query: tuple[str, ...],
    tag: tuple[str, ...],
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Fetch markets and find structural spread pairs.

    Searches multiple keywords and/or tags, merges unique markets, then runs
    deterministic rules to find structural pairs (temporal implication,
    cross-platform match).

    Returns both the full market list and discovered pairs so the agent can
    do its own semantic analysis on the raw data.

    \b
      coinjure market discover -q "election" -q "Trump" --json
      coinjure market discover -t Crypto -t Finance --exchange polymarket --json
      coinjure market discover -q "tariff" -t Economy --exchange both --json
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
                    _merge_poly(await _polymarket_search_markets(q, limit))
                if exchange in ('kalshi', 'both'):
                    _merge_kalshi(
                        await _kalshi_search_markets(
                            q,
                            limit,
                            kalshi_api_key_id,
                            kalshi_private_key_path,
                        )
                    )
            for t in tag:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(await _polymarket_list_markets(limit, tag=t))
        else:
            if exchange in ('polymarket', 'both'):
                poly_markets = await _polymarket_list_markets(limit)
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

    # ── Structural rules ───────────────────────────────────────────────

    rule_pairs = _discover_rules(poly_markets, kalshi_markets)

    # ── Persist pairs to relation store ────────────────────────────────

    from coinjure.market.relations import MarketRelation, RelationStore

    store = RelationStore()
    saved_count = 0
    for p in rule_pairs:
        ma = p.get('market_a', {})
        mb = p.get('market_b', {})
        rid = f'{ma.get("id", "a")[:8]}-{mb.get("id", "b")[:8]}'
        rel = MarketRelation(
            relation_id=rid,
            market_a=ma,
            market_b=mb,
            spread_type=p.get('spread_type', 'unknown'),
            confidence=float(p.get('confidence', 0)),
            reasoning=p.get('reasoning', ''),
            hypothesis=p.get('hypothesis', ''),
        )
        store.add(rel)
        saved_count += 1

    # ── Output ─────────────────────────────────────────────────────────

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'markets': {
                        'polymarket': poly_markets,
                        'kalshi': kalshi_markets,
                    },
                    'total_markets': len(poly_markets) + len(kalshi_markets),
                    'structural_pairs': rule_pairs,
                    'saved': saved_count,
                },
                default=str,
            )
        )
        return

    parts = [f'"{q}"' for q in query] + [f'tag:{t}' for t in tag]
    query_desc = ', '.join(parts) if parts else 'top by volume'
    click.echo(
        f'Fetched {len(poly_markets)} Polymarket + {len(kalshi_markets)} Kalshi '
        f'markets (queries: {query_desc}).'
    )

    if rule_pairs:
        click.echo(f'\n{len(rule_pairs)} structural pairs found:\n')
        for i, p in enumerate(rule_pairs, 1):
            ma = p.get('market_a', {})
            mb = p.get('market_b', {})
            click.echo(
                f'  {i}. [{p.get("spread_type", "?")}] '
                f'confidence={p.get("confidence", "?")}'
            )
            click.echo(f'     A: [{ma.get("platform", "?")}] {ma.get("question", "?")}')
            click.echo(f'     B: [{mb.get("platform", "?")}] {mb.get("question", "?")}')
            if p.get('hypothesis'):
                click.echo(f'     Hypothesis: {p["hypothesis"]}')
            click.echo(f'     {p.get("reasoning", "")}')
            click.echo()
    else:
        click.echo('\nNo structural pairs found.')

    click.echo(
        f'Total {len(poly_markets) + len(kalshi_markets)} markets returned. '
        f'Use --json to get full data for agent analysis.'
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Polymarket price history
# ---------------------------------------------------------------------------

_INTERVAL_FIDELITY: dict[str, int] = {
    'max': 1440,
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
