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


async def _polymarket_list_markets(limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            GAMMA_EVENTS_URL,
            params={'active': 'true', 'closed': 'false', 'limit': min(limit, 100)},
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    events = resp.json()
    markets: list[dict[str, Any]] = []
    for event in events[:limit]:
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
                }
            )
        if len(markets) >= limit:
            break
    return markets[:limit]


async def _polymarket_search_markets(query: str, limit: int) -> list[dict]:
    all_markets = await _polymarket_list_markets(500)
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
    if key_id and pk_path:
        with open(pk_path) as f:
            config.private_key_pem = f.read()
        config.api_key_id = key_id

    api_client = ApiClient(configuration=config)
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


async def _kalshi_search_markets(
    query: str, limit: int, api_key_id: str | None, private_key_path: str | None
) -> list[dict]:
    all_markets = await _kalshi_list_markets(500, api_key_id, private_key_path)
    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('title', '').lower() or q in m.get('ticker', '').lower()
    ]
    return filtered[:limit]


async def _kalshi_market_info(
    market_ticker: str, api_key_id: str | None, private_key_path: str | None
) -> dict | None:
    from kalshi_python import Configuration
    from kalshi_python.api.markets_api import MarketsApi
    from kalshi_python.api_client import ApiClient

    config = Configuration(host=KALSHI_API_URL)
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if key_id and pk_path:
        with open(pk_path) as f:
            config.private_key_pem = f.read()
        config.api_key_id = key_id

    api_client = ApiClient(configuration=config)
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


def _fmt_poly_market(m: dict, idx: int) -> str:
    lines = [f'[{idx}] {m.get("question", "(no question)")}']
    event = m.get('event_title')
    if event and event != m.get('question'):
        lines.append(f'     Event:    {event}')
    lines.append(f'     Market ID: {m.get("id", "")}')
    bid = m.get('best_bid', '')
    ask = m.get('best_ask', '')
    if bid or ask:
        lines.append(f'     Bid/Ask:  {bid} / {ask}')
    if m.get('volume'):
        lines.append(f'     Volume:   {m["volume"]}')
    if m.get('end_date'):
        lines.append(f'     Closes:   {m["end_date"]}')
    return '\n'.join(lines)


def _fmt_kalshi_market(m: dict, idx: int) -> str:
    bid_cents = m.get('yes_bid', 0) or 0
    ask_cents = m.get('yes_ask', 0) or 0
    bid_pct = f'{bid_cents}¢'
    ask_pct = f'{ask_cents}¢'
    lines = [f'[{idx}] {m.get("title", "(no title)")}']
    lines.append(f'     Ticker:   {m.get("ticker", "")}')
    lines.append(f'     Event:    {m.get("event_ticker", "")}')
    lines.append(f'     Bid/Ask:  {bid_pct} / {ask_pct}')
    if m.get('volume'):
        lines.append(f'     Volume:   {m["volume"]}')
    if m.get('close_time'):
        lines.append(f'     Closes:   {m["close_time"]}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def market() -> None:
    """Explore prediction markets on Polymarket and Kalshi."""


@market.command('list')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option(
    '--kalshi-api-key-id',
    default=None,
    help='Kalshi API key id (or KALSHI_API_KEY_ID).',
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_list(
    exchange: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """List open markets on a prediction exchange."""
    try:
        if exchange == 'polymarket':
            markets = asyncio.run(_polymarket_list_markets(limit))
        else:
            markets = asyncio.run(
                _kalshi_list_markets(limit, kalshi_api_key_id, kalshi_private_key_path)
            )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {'exchange': exchange, 'count': len(markets), 'markets': markets}
            )
        )
        return

    if not markets:
        click.echo('No markets found.')
        return

    click.echo(f'Listing {len(markets)} open market(s) on {exchange}:\n')
    for i, m in enumerate(markets, 1):
        if exchange == 'polymarket':
            click.echo(_fmt_poly_market(m, i))
        else:
            click.echo(_fmt_kalshi_market(m, i))
        click.echo()


@market.command('search')
@click.option(
    '--query', required=True, help='Keyword to search in market title/question.'
)
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--kalshi-api-key-id', default=None)
@click.option('--kalshi-private-key-path', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_search(
    query: str,
    exchange: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Search markets by keyword."""
    try:
        if exchange == 'polymarket':
            markets = asyncio.run(_polymarket_search_markets(query, limit))
        else:
            markets = asyncio.run(
                _kalshi_search_markets(
                    query, limit, kalshi_api_key_id, kalshi_private_key_path
                )
            )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to search markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'exchange': exchange,
                    'query': query,
                    'count': len(markets),
                    'markets': markets,
                }
            )
        )
        return

    if not markets:
        click.echo(f'No markets found matching {query!r}.')
        return

    click.echo(f'Found {len(markets)} market(s) matching {query!r} on {exchange}:\n')
    for i, m in enumerate(markets, 1):
        if exchange == 'polymarket':
            click.echo(_fmt_poly_market(m, i))
        else:
            click.echo(_fmt_kalshi_market(m, i))
        click.echo()


@market.command('info')
@click.option('--market-id', required=True, help='Market ID or ticker to inspect.')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--kalshi-api-key-id', default=None)
@click.option('--kalshi-private-key-path', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_info(
    market_id: str,
    exchange: str,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Show detailed info and top-of-book for a specific market."""
    try:
        if exchange == 'polymarket':
            info = asyncio.run(_polymarket_market_info(market_id))
        else:
            info = asyncio.run(
                _kalshi_market_info(
                    market_id, kalshi_api_key_id, kalshi_private_key_path
                )
            )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch market info: {exc}') from exc

    if info is None:
        raise click.ClickException(f'Market not found: {market_id}')

    if as_json:
        click.echo(json.dumps({'exchange': exchange, 'market': info}))
        return

    click.echo(f'\nMarket Info ({exchange})')
    click.echo('=' * 60)
    if exchange == 'polymarket':
        click.echo(f'Question:   {info.get("question", "")}')
        click.echo(f'Market ID:  {info.get("id", "")}')
        click.echo(f'Event ID:   {info.get("event_id", "")}')
        click.echo(f'Token ID:   {info.get("token_id", "")}')
        bid = info.get('best_bid', '')
        ask = info.get('best_ask', '')
        click.echo(f'Bid / Ask:  {bid} / {ask}')
        click.echo(f'Volume:     {info.get("volume", "")}')
        click.echo(f'Closes:     {info.get("end_date", "")}')
        click.echo(f'Active:     {info.get("active", True)}')
        desc = info.get('description', '')
        if desc:
            click.echo(f'Description: {desc[:300]}{"…" if len(desc) > 300 else ""}')
    else:
        click.echo(f'Title:      {info.get("title", "")}')
        click.echo(f'Ticker:     {info.get("ticker", "")}')
        click.echo(f'Event:      {info.get("event_ticker", "")}')
        bid_c = info.get('yes_bid', 0) or 0
        ask_c = info.get('yes_ask', 0) or 0
        click.echo(f'Bid / Ask:  {bid_c}¢ / {ask_c}¢')
        click.echo(f'Volume:     {info.get("volume", 0)}')
        click.echo(f'Closes:     {info.get("close_time", "")}')
        click.echo(f'Status:     {info.get("status", "")}')
        rules = info.get('rules_primary', '')
        if rules:
            click.echo(f'Rules:      {rules[:300]}{"…" if len(rules) > 300 else ""}')
    click.echo()


# ---------------------------------------------------------------------------
# market match (fuzzy-match across platforms)
# ---------------------------------------------------------------------------


@market.command('match')
@click.option('--query', required=True, help='Keyword to search on both platforms.')
@click.option('--min-similarity', default='0.6', show_default=True)
@click.option('--limit', default=50, show_default=True, type=int)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_match(
    query: str,
    min_similarity: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Fuzzy-match markets across Polymarket and Kalshi by keyword."""
    from coinjure.cli.arb_helpers import _pair_ids_in_portfolio
    from coinjure.market.matching import MarketPair, match_markets

    try:
        min_sim = float(min_similarity)
    except ValueError as exc:
        raise click.ClickException(f'Invalid --min-similarity: {exc}') from exc

    async def _fetch() -> list[MarketPair]:
        poly_markets, kalshi_markets = await asyncio.gather(
            _polymarket_search_markets(query, limit),
            _kalshi_search_markets(
                query, limit, kalshi_api_key_id, kalshi_private_key_path
            ),
        )
        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        return pairs

    try:
        pairs = asyncio.run(_fetch())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to match markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'query': query,
                    'count': len(pairs),
                    'pairs': [
                        {
                            'poly': p.poly,
                            'kalshi': p.kalshi,
                            'similarity': p.similarity,
                            'already_in_portfolio': p.already_in_portfolio,
                        }
                        for p in pairs
                    ],
                },
                default=str,
            )
        )
        return

    if not pairs:
        click.echo(f'No matching market pairs found for {query!r}.')
        return

    click.echo(
        f'\nMarket pairs matching {query!r}  (min_similarity={min_similarity}):\n'
    )
    for p in pairs:
        already = ' [IN PORTFOLIO]' if p.already_in_portfolio else ''
        click.echo(f'  sim={p.similarity:.3f}{already}')
        click.echo(f'  poly:   {p.poly.get("question", "")[:70]}')
        click.echo(
            f'  kalshi: {p.kalshi.get("title", "")[:70]}  [{p.kalshi.get("ticker", "")}]'
        )
        click.echo()


# ---------------------------------------------------------------------------
# arb-scan (cross-platform arb scanning)
# ---------------------------------------------------------------------------


@market.command('scan')
@click.option(
    '--query', required=True, help='Keyword to search markets on both platforms.'
)
@click.option(
    '--min-edge', default='0.02', show_default=True, help='Minimum gross edge (0-1).'
)
@click.option(
    '--min-similarity',
    default='0.6',
    show_default=True,
    help='Minimum fuzzy-match similarity.',
)
@click.option(
    '--limit',
    default=50,
    show_default=True,
    type=int,
    help='Max markets to fetch per exchange.',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_scan(
    query: str,
    min_edge: str,
    min_similarity: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Scan for live cross-platform arb opportunities matching a keyword."""
    from decimal import Decimal, InvalidOperation

    from coinjure.cli.arb_helpers import _compute_edge, _pair_ids_in_portfolio
    from coinjure.market.matching import match_markets

    try:
        min_edge_dec = Decimal(min_edge)
        min_sim = float(min_similarity)
    except (InvalidOperation, ValueError) as exc:
        raise click.ClickException(f'Invalid numeric argument: {exc}') from exc

    async def _fetch_and_scan() -> list[dict]:
        poly_task = _polymarket_search_markets(query, limit)
        kalshi_task = _kalshi_search_markets(
            query, limit, kalshi_api_key_id, kalshi_private_key_path
        )
        poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        opportunities: list[dict] = []
        for pair in pairs:
            edge_info = _compute_edge(pair)
            if edge_info is None:
                continue
            if Decimal(edge_info['edge']) >= min_edge_dec:
                opportunities.append(edge_info)

        opportunities.sort(key=lambda x: Decimal(x['edge']), reverse=True)
        return opportunities

    try:
        opportunities = asyncio.run(_fetch_and_scan())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {'ok': True, 'query': query, 'opportunities': opportunities},
                default=str,
            )
        )
        return

    if not opportunities:
        click.echo(f'No arb opportunities found for {query!r} with edge >= {min_edge}.')
        return

    click.echo(f'\nArb scan: {query!r}  (min_edge={min_edge})\n')
    for opp in opportunities:
        already = ' [IN PORTFOLIO]' if opp['already_in_portfolio'] else ''
        click.echo(
            f'  edge={float(opp["edge"]):.3f} net={float(opp["edge_net"]):.3f}'
            f'  sim={opp["similarity"]:.2f}{already}'
        )
        click.echo(f'  poly:  {opp["poly_question"][:60]}')
        click.echo(f'  kalshi ticker: {opp["kalshi_ticker"]}')
        click.echo(f'  poly_ask={opp["poly_ask"]} kalshi_ask={opp["kalshi_ask"]}')
        click.echo(f'  {opp["rationale"]}')
        click.echo()


# ---------------------------------------------------------------------------
# arb-scan-events (intra-event sum arb scanning)
# ---------------------------------------------------------------------------


@market.command('scan-events')
@click.option(
    '--query', default='', help='Keyword to filter event titles (empty = all).'
)
@click.option(
    '--min-edge',
    default='0.01',
    show_default=True,
    help='Minimum net edge after fees (0-1).',
)
@click.option(
    '--min-markets',
    default=2,
    show_default=True,
    type=int,
    help='Minimum number of priced markets in the event.',
)
@click.option(
    '--limit',
    default=20,
    show_default=True,
    type=int,
    help='Max opportunities to return.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_scan_events(
    query: str,
    min_edge: str,
    min_markets: int,
    limit: int,
    as_json: bool,
) -> None:
    """Scan Polymarket events for intra-event sum(YES) arbitrage.

    Finds multi-outcome events where the sum of YES ask prices deviates
    from 1.0 by more than fees, creating a risk-free arb opportunity.

    Example:

        coinjure market arb-scan-events --query "NBA" --min-edge 0.01 --json
    """
    from decimal import Decimal, InvalidOperation

    from coinjure.cli.arb_helpers import _fetch_event_sum_opportunities

    try:
        min_edge_dec = Decimal(min_edge)
    except InvalidOperation as exc:
        raise click.ClickException(f'Invalid --min-edge: {exc}') from exc

    try:
        opportunities = asyncio.run(
            _fetch_event_sum_opportunities(query, limit * 4, min_edge_dec, min_markets)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    opportunities = opportunities[:limit]

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'query': query,
                    'count': len(opportunities),
                    'opportunities': opportunities,
                },
                default=str,
            )
        )
        return

    if not opportunities:
        click.echo(
            f'No event-sum arb opportunities found (query={query!r}, min_edge={min_edge}).'
        )
        return

    click.echo(f'\nEvent-sum arb scan  query={query!r}  min_edge={min_edge}\n')
    for opp in opportunities:
        click.echo(
            f'  edge={opp["best_edge"]}  action={opp["action"]}'
            f'  n={opp["n_markets"]}  sum_yes={opp["sum_yes"]}'
        )
        click.echo(f'  event: {opp["event_title"]}')
        click.echo(f'  event_id: {opp["event_id"]}')
        click.echo()


# ---------------------------------------------------------------------------
# Polymarket price history
# ---------------------------------------------------------------------------

_INTERVAL_FIDELITY: dict[str, int] = {
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


# ---------------------------------------------------------------------------
# market record (absorbed from data_commands.py)
# ---------------------------------------------------------------------------


@market.command('record')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
    help='Exchange to record from.',
)
@click.option(
    '--output',
    default='events.jsonl',
    show_default=True,
    type=click.Path(dir_okay=False),
    help='Output JSONL file path (appended if it already exists).',
)
@click.option(
    '--duration',
    default=None,
    type=float,
    help='How many seconds to record (default: run until Ctrl-C).',
)
@click.option(
    '--polling-interval',
    default=60.0,
    show_default=True,
    type=float,
    help='Polling interval in seconds for market data sources.',
)
@click.option(
    '--kalshi-api-key-id',
    default=None,
    help='Kalshi API key id (or KALSHI_API_KEY_ID).',
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH).',
)
@click.option(
    '--verbose', '-v', is_flag=True, default=False, help='Print each recorded event.'
)
@click.option(
    '--json',
    'as_json',
    is_flag=True,
    default=False,
    help='Emit JSON status on completion.',
)
def market_record(
    exchange: str,
    output: str,
    duration: float | None,
    polling_interval: float,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Record live market events to a JSONL file for later backtesting.

    Events are written in a format compatible with ``strategy backtest``.
    Press Ctrl-C to stop recording early.
    """
    from pathlib import Path

    from coinjure.cli.data_commands import _record_loop
    from coinjure.market.live.kalshi_data_source import LiveKalshiDataSource
    from coinjure.market.live.live_data_source import LivePolyMarketDataSource

    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if exchange == 'polymarket':
        data_source = LivePolyMarketDataSource(
            event_cache_file='record_events_cache.jsonl',
            polling_interval=polling_interval,
            orderbook_refresh_interval=min(polling_interval, 10.0),
            reprocess_on_start=True,
        )
    else:
        data_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='record_kalshi_cache.jsonl',
            polling_interval=polling_interval,
            reprocess_on_start=True,
        )

    dur_msg = f'{duration}s' if duration else 'until Ctrl-C'
    if not as_json:
        click.echo(f'Recording {exchange} events → {output_path}  ({dur_msg})')
        click.echo('Press Ctrl-C to stop.\n')

    count = 0
    try:
        count = asyncio.run(_record_loop(data_source, output_path, duration, verbose))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise click.ClickException(f'Recording failed: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'exchange': exchange,
                    'output': str(output_path),
                    'events_recorded': count,
                }
            )
        )
    else:
        click.echo(f'\nRecording complete: {count} event(s) written to {output_path}')


# ---------------------------------------------------------------------------
# market snapshot (absorbed from portfolio_commands.py)
# ---------------------------------------------------------------------------


@market.command('snapshot')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--query', 'search_query', default=None, help='Optional search filter.')
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_snapshot(
    exchange: str,
    search_query: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """One-shot market intelligence: movers, arb edges, portfolio & memory overlap."""
    from datetime import datetime, timezone

    from coinjure.portfolio.registry import StrategyRegistry
    from coinjure.research.ledger import ExperimentLedger

    snapshot: dict[str, Any] = {
        'ok': True,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'exchange': exchange,
    }

    markets: list[dict[str, Any]] = []
    try:
        if exchange == 'polymarket':
            from coinjure.market.live.live_data_source import LivePolyMarketDataSource

            ds = LivePolyMarketDataSource(polling_interval=0)
            raw_markets = (
                asyncio.get_event_loop().run_until_complete(ds._fetch_markets())
                if hasattr(ds, '_fetch_markets')
                else []
            )
            for m in raw_markets[:limit]:
                markets.append(
                    {
                        'market_id': getattr(m, 'market_id', str(m)),
                        'title': getattr(m, 'question', getattr(m, 'title', '')),
                    }
                )
    except Exception:  # noqa: BLE001
        snapshot['markets_error'] = 'Failed to fetch live markets'

    snapshot['markets_count'] = len(markets)

    try:
        registry = StrategyRegistry()
        active = [
            e.to_dict()
            for e in registry.list()
            if e.lifecycle in ('paper_trading', 'live_trading')
        ]
        snapshot['active_portfolio'] = active
        snapshot['active_count'] = len(active)
    except Exception:  # noqa: BLE001
        snapshot['active_portfolio'] = []
        snapshot['active_count'] = 0

    try:
        ledger = ExperimentLedger()
        summary = ledger.summary()
        recent_best = ledger.best(metric_key='total_pnl', top_n=5)
        snapshot['memory_summary'] = summary
        snapshot['memory_top5'] = [
            {
                'run_id': e.run_id,
                'strategy_ref': e.strategy_ref,
                'market_id': e.market_id,
                'gate_passed': e.gate_passed,
                'pnl': e.metrics.get('total_pnl'),
            }
            for e in recent_best
        ]
    except Exception:  # noqa: BLE001
        snapshot['memory_summary'] = {'total_experiments': 0}
        snapshot['memory_top5'] = []

    if as_json:
        click.echo(json.dumps(snapshot, default=str))
    else:
        click.echo(json.dumps(snapshot, indent=2, default=str))
