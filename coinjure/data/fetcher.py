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
                    raise ValueError(
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
                if len(markets) >= limit:
                    break
            offset += len(events)
            if len(events) < page_size:
                break  # No more pages

    return markets[:limit]


async def polymarket_search_markets(
    query: str,
    limit: int,
    *,
    tag: str | None = None,
    with_rules: bool = False,
) -> list[dict]:
    all_markets = await polymarket_list_markets(500, tag=tag, with_rules=with_rules)
    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('question', '').lower() or q in m.get('event_title', '').lower()
    ]
    return filtered[:limit]


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


async def kalshi_search_via_events(
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


async def kalshi_search_markets(
    query: str,
    limit: int,
    api_key_id: str | None,
    private_key_path: str | None,
    with_rules: bool = False,
) -> list[dict]:
    return await kalshi_search_via_events(query, limit, with_rules=with_rules)


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
