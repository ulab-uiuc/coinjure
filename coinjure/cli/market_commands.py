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
    markets = []
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
                    'token_id': mkt.get('clob_token_ids', [''])[0]
                    if mkt.get('clob_token_ids')
                    else '',
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

    return {
        'id': mkt.get('id', ''),
        'question': mkt.get('question', ''),
        'event_id': str(mkt.get('eventId', '')),
        'token_id': mkt.get('clob_token_ids', [''])[0]
        if mkt.get('clob_token_ids')
        else '',
        'no_token_id': mkt.get('clob_token_ids', ['', ''])[1]
        if len(mkt.get('clob_token_ids', [])) > 1
        else '',
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
    d = m.to_dict() if hasattr(m, 'to_dict') else dict(m)
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
