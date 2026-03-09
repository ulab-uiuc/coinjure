"""API fetching functions for Polymarket (Gamma API) and Kalshi."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Polymarket (via Gamma API — no auth required)
# ---------------------------------------------------------------------------

GAMMA_EVENTS_URL = 'https://gamma-api.polymarket.com/events'
GAMMA_MARKETS_URL = 'https://gamma-api.polymarket.com/markets'

# ---------------------------------------------------------------------------
# Kalshi
# ---------------------------------------------------------------------------

KALSHI_API_URL = 'https://api.elections.kalshi.com/trade-api/v2'


def parse_clob_ids(mkt: dict) -> list[str]:
    """Return the parsed list of CLOB token IDs from a market dict (handles JSON-string encoding)."""
    raw = mkt.get('clobTokenIds') or mkt.get('clob_token_ids') or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = []
    return list(raw)


async def polymarket_list_markets(
    limit: int,
    *,
    tag: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    """Fetch top markets by volume (parallel pages). Alias for sorted fetch."""
    return await _polymarket_list_markets_sorted(
        limit, 'volume', False, tag, with_rules
    )


def _parse_events_to_markets(events: list[dict], with_rules: bool) -> list[dict]:
    """Convert Gamma API event list to flat market list."""
    markets: list[dict] = []
    for event in events:
        tags_list = [
            t.get('label', '') for t in event.get('tags', []) if t.get('label')
        ]
        category = event.get('category', '')
        for mkt in event.get('markets', []):
            clob_ids = parse_clob_ids(mkt)
            entry: dict[str, Any] = {
                'id': mkt.get('id', ''),
                'question': mkt.get('question', ''),
                'event_id': str(event.get('id', '')),
                'event_title': event.get('title', ''),
                'token_ids': clob_ids,
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
    return markets


async def _polymarket_list_markets_sorted(
    limit: int,
    order: str,
    ascending: bool,
    tag: str | None,
    with_rules: bool,
) -> list[dict]:
    """Fetch markets with a specific sort order, using parallel page requests."""
    page_size = 100
    num_pages = (limit + page_size - 1) // page_size

    base_params: dict[str, Any] = {
        'active': 'true',
        'closed': 'false',
        'limit': page_size,
        'order': order,
        'ascending': 'true' if ascending else 'false',
    }
    if tag:
        base_params['tag'] = tag

    async def fetch_page(client: httpx.AsyncClient, offset: int) -> list[dict]:
        resp = await client.get(
            GAMMA_EVENTS_URL, params={**base_params, 'offset': offset}
        )
        if resp.status_code != 200:
            return []
        return resp.json() or []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch first page to check if data exists, then fetch rest in parallel
        first_events = await fetch_page(client, 0)
        if not first_events:
            return []

        if num_pages == 1:
            return _parse_events_to_markets(first_events, with_rules)[:limit]

        # Fetch remaining pages in parallel
        offsets = [i * page_size for i in range(1, num_pages)]
        pages = await asyncio.gather(*[fetch_page(client, off) for off in offsets])

    all_events = first_events + [ev for page in pages for ev in page]
    return _parse_events_to_markets(all_events, with_rules)[:limit]


async def polymarket_search_markets(
    query: str,
    limit: int,
    *,
    tag: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    # Fetch from two sort orders: top by volume AND newest by startDate.
    # Fetch top 2500 by volume (parallel pages, ~2s) to cover mid-tier markets like Fed (~rank 1961).
    # Also fetch 300 newest to catch recently-created markets.
    by_volume, by_recent = await asyncio.gather(
        _polymarket_list_markets_sorted(2500, 'volume', False, tag, with_rules),
        _polymarket_list_markets_sorted(300, 'startDate', False, tag, with_rules),
    )
    # Merge, deduplicate by market id
    seen: set[str] = set()
    all_markets: list[dict] = []
    for m in by_volume + by_recent:
        mid = str(m.get('id', ''))
        if mid and mid not in seen:
            seen.add(mid)
            all_markets.append(m)

    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('question', '').lower() or q in m.get('event_title', '').lower()
    ]
    return filtered[:limit]


async def polymarket_fetch_by_slug(
    slug: str, *, with_rules: bool = False
) -> list[dict]:
    """Fetch all markets in a Polymarket event by its URL slug."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(GAMMA_EVENTS_URL, params={'slug': slug})
    if resp.status_code != 200:
        return []
    events = resp.json()
    if not events:
        return []
    return _parse_events_to_markets(events, with_rules)


async def polymarket_market_info(market_id: str) -> dict | None:
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

    clob_ids = parse_clob_ids(mkt)
    # eventId is often None in Gamma Markets API; fall back to events[0].id
    event_id = mkt.get('eventId') or ''
    if not event_id:
        events = mkt.get('events') or []
        if events and isinstance(events, list):
            event_id = events[0].get('id', '')
    return {
        'id': mkt.get('id', ''),
        'question': mkt.get('question', ''),
        'event_id': str(event_id),
        'token_ids': clob_ids,
        'best_bid': mkt.get('bestBid', ''),
        'best_ask': mkt.get('bestAsk', ''),
        'volume': mkt.get('volume', ''),
        'end_date': mkt.get('endDate', ''),
        'description': mkt.get('description', ''),
        'active': mkt.get('active', True),
        'closed': mkt.get('closed', False),
    }


async def kalshi_list_markets(
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


async def kalshi_search_markets(
    query: str,
    limit: int,
    api_key_id: str | None = None,
    private_key_path: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    """Search Kalshi events by keyword via raw HTTP and extract nested markets.

    Scans all pages (up to 4000 events) before trimming to limit, so that
    markets on later pages (e.g. KXFEDDECISION-26APR) are not missed.
    """
    q = query.lower()
    matched_markets: list[dict] = []
    cursor: str | None = None
    pages = 0
    max_pages = 20  # up to 4 000 events

    async with httpx.AsyncClient(timeout=30.0) as client:
        while pages < max_pages:
            params: dict[str, Any] = {
                'status': 'open',
                'limit': 200,
                'with_nested_markets': 'true',
            }
            if cursor:
                params['cursor'] = cursor

            resp = await client.get(f'{KALSHI_API_URL}/events', params=params)
            if resp.status_code != 200:
                break

            data = resp.json()
            events = data.get('events', [])
            if not events:
                break

            for event in events:
                if q not in event.get('title', '').lower():
                    continue
                for mkt in event.get('markets', []) or []:
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

            cursor = data.get('cursor')
            pages += 1
            if not cursor:
                break

    # Sort by volume descending so highest-liquidity markets surface first
    matched_markets.sort(key=lambda m: m.get('volume', 0) or 0, reverse=True)
    return matched_markets[:limit]


async def kalshi_market_info(
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
