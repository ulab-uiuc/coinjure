import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import feedparser
import httpx
from py_clob_client.client import ClobClient

from coinjure.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.ticker import PolyMarketTicker

from ..source import DataSource

logger = logging.getLogger(__name__)


class _Level:
    """Duck-typed order book level matching py_clob_client's interface."""

    __slots__ = ('price', 'size')

    def __init__(self, price: str, size: str) -> None:
        self.price = price
        self.size = size


class _OrderBookResult:
    """Duck-typed order book matching py_clob_client's interface."""

    def __init__(self, data: dict) -> None:
        self.bids = [_Level(b['price'], b['size']) for b in data.get('bids', [])]
        self.asks = [_Level(a['price'], a['size']) for a in data.get('asks', [])]


CLOB_PRICES_HISTORY_URL = 'https://clob.polymarket.com/prices-history'


async def fetch_price_history(
    token_id: str,
    fidelity: int = 60,
    start_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch CLOB price history for a single token.

    Returns list of ``{'t': <unix_timestamp>, 'p': <price_string>}``.

    Args:
        token_id: CLOB token ID (long hex string).
        fidelity: Minutes per candle (1, 5, 60, 1440).
        start_ts: Optional Unix start timestamp.
    """
    params: dict[str, Any] = {
        'market': token_id,
        'interval': 'all',
        'fidelity': fidelity,
    }
    if start_ts is not None:
        params['startTs'] = start_ts

    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(), timeout=30.0
    ) as client:
        resp = await client.get(CLOB_PRICES_HISTORY_URL, params=params)
    if resp.status_code != 200:
        raise ValueError(
            f'CLOB prices-history returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    data = resp.json()
    raw_history = data.get('history') if isinstance(data, dict) else data
    points: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if isinstance(item, dict) and 't' in item and 'p' in item:
                points.append({'t': item['t'], 'p': item['p']})
    return points


class LivePolyMarketDataSource(DataSource):
    event_cache_file: str
    polling_interval: float
    processed_event_ids: set[str]
    event_queue: asyncio.Queue
    clob_client: ClobClient
    last_order_book_state: dict[str, Decimal]

    def __init__(
        self,
        event_cache_file: str = 'events_cache.jsonl',
        polling_interval: float = 60.0,
        orderbook_refresh_interval: float = 10.0,
        reprocess_on_start: bool = True,
    ):
        self.event_cache_file = event_cache_file
        self.polling_interval = polling_interval
        self.orderbook_refresh_interval = orderbook_refresh_interval
        self.processed_event_ids: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clob_client = ClobClient('https://clob.polymarket.com')
        self.last_order_book_state: dict[str, Decimal] = {}
        self._poll_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._first_poll = True
        self._reprocess_on_start = reprocess_on_start
        # Track tickers with actual liquidity for periodic refresh
        self._known_tickers: dict[str, PolyMarketTicker] = {}  # token_id → ticker
        self._last_refresh_time: dict[str, float] = {}  # token_id → timestamp
        self._max_known_tickers: int = 500
        # Priority tokens (positions we hold) — always refreshed first
        self._priority_tokens: set[str] = set()
        # Track last mid-price per token to derive PriceChangeEvents
        self._last_mid_price: dict[str, Decimal] = {}
        # Rotation offset for non-priority token refresh batches
        self._refresh_offset: int = 0
        # Semaphore to limit concurrent order book fetches
        self._fetch_semaphore = asyncio.Semaphore(5)
        # Persistent async HTTP client for order book fetches (created in start())
        self._http_client: httpx.AsyncClient | None = None

        # Load and deduplicate cache file on startup (Fix 5)
        self._cached_event_ids: set[str] = set()
        if os.path.exists(self.event_cache_file):
            seen_ids: set[str] = set()
            unique_lines: list[str] = []
            with open(self.event_cache_file) as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        eid = str(event.get('id', ''))
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            unique_lines.append(line.strip())
                    except json.JSONDecodeError:
                        continue
            # Rewrite deduped cache file
            with open(self.event_cache_file, 'w') as f:
                for line in unique_lines:
                    f.write(line + '\n')
            self._cached_event_ids = seen_ids

        if not reprocess_on_start:
            self.processed_event_ids = set(self._cached_event_ids)
            # Pre-register tickers from cached events so the refresh loop
            # can use them immediately (instead of waiting for the poll to
            # re-fetch all events from the API).
            self._register_tickers_from_cache()

    def _register_tickers_from_cache(self) -> None:
        """Register tickers from the local cache file (synchronous).

        Called during __init__ when reprocess_on_start=False so that
        the refresh loop has proper tickers immediately on start().
        """
        if not os.path.exists(self.event_cache_file):
            return
        with open(self.event_cache_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                event_id = str(event.get('id', ''))
                for market in event.get('markets', []):
                    market_title = market.get('question', market.get('title', ''))
                    market_id = market.get('id', '')
                    token_ids = json.loads(market.get('clobTokenIds', '[]'))
                    for idx, token_id in enumerate(token_ids):
                        self._known_tickers[token_id] = PolyMarketTicker(
                            symbol=token_id,
                            name=market_title,
                            token_id=token_id,
                            market_id=market_id,
                            event_id=event_id,
                            side='no' if idx == 1 else 'yes',
                        )

    async def _fetch_events(self) -> list[dict[str, Any]]:
        all_events: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(), timeout=30.0
        ) as client:
            while True:
                response = await client.get(
                    'https://gamma-api.polymarket.com/events',
                    params={
                        'active': 'true',
                        'closed': 'false',
                        'limit': limit,
                        'offset': offset,
                    },
                )
                if response.status_code != 200:
                    break
                events = response.json()
                if not events:
                    break
                all_events.extend(events)
                if len(events) < limit:
                    break  # Last page
                # On subsequent polls (not first), only fetch first page for new events.
                # Even on first poll, if we already have a substantial cache,
                # limit to first 3 pages — the full scan takes too long and
                # blocks the event loop from processing priority tokens.
                if not self._first_poll:
                    break
                if len(self.processed_event_ids) > 100 and offset >= 300:
                    break
                offset += limit
        return all_events

    def _fetch_order_book_sync(self, token_id: str) -> Any:
        """Synchronous order book fetch (runs in thread pool)."""
        try:
            return self.clob_client.get_order_book(token_id)
        except Exception:
            return None

    async def _fetch_order_book(self, token_id: str) -> Any:
        """Non-blocking order book fetch via persistent httpx client."""
        client = self._http_client
        if client is None:
            return None
        try:
            resp = await client.get(
                'https://clob.polymarket.com/book',
                params={'token_id': token_id},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return _OrderBookResult(data)
        except Exception:
            return None

    def _process_order_book_to_events(
        self,
        token_id: str,
        market_title: str,
        order_book: Any,
        market_id: str = '',
        event_id: str = '',
        side: str = 'yes',
    ) -> list[OrderBookEvent]:
        events = []

        if not order_book:
            return events

        # Create PolyMarketTicker with all available IDs
        ticker = PolyMarketTicker(
            symbol=token_id,
            name=market_title,
            token_id=token_id,
            market_id=market_id,
            event_id=event_id,
            side=side,
        )

        # Only track tickers that have actual liquidity for refresh
        if order_book.bids or order_book.asks:
            self._known_tickers[token_id] = ticker
            self._last_refresh_time[token_id] = time.monotonic()

        for bid in order_book.bids:
            price = Decimal(bid.price)
            size = Decimal(bid.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{bid.price}:bid'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker,
                    price=price,
                    size=size,
                    size_delta=size_delta,
                    side='bid',
                )
                events.append(event)

        for ask in order_book.asks:
            price = Decimal(ask.price)
            size = Decimal(ask.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{ask.price}:ask'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker,
                    price=price,
                    size=size,
                    size_delta=size_delta,
                    side='ask',
                )
                events.append(event)

        # Derive PriceChangeEvent from best bid/ask mid-price
        best_bid = max(
            (Decimal(b.price) for b in order_book.bids),
            default=None,
        )
        best_ask = min(
            (Decimal(a.price) for a in order_book.asks),
            default=None,
        )
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        else:
            mid = None

        if mid is not None:
            prev_mid = self._last_mid_price.get(token_id)
            if prev_mid is None or mid != prev_mid:
                self._last_mid_price[token_id] = mid
                events.append(PriceChangeEvent(ticker=ticker, price=mid))

        return events

    async def _poll_data(self) -> None:
        while True:
            try:
                events = await self._fetch_events()

                for event in events:
                    event_id = str(event.get('id'))
                    if event_id not in self.processed_event_ids:
                        enriched_event = event

                        for market in enriched_event.get('markets', []):
                            market_title = market.get(
                                'question', market.get('title', '')
                            )
                            market_id = market.get('id', '')
                            token_ids = json.loads(market.get('clobTokenIds', '[]'))

                            # Convention: clobTokenIds[0] = YES, [1] = NO
                            yes_token_id = token_ids[0] if len(token_ids) > 0 else ''

                            # Fetch order books for new events
                            for idx, token_id in enumerate(token_ids):
                                order_book = await self._fetch_order_book(token_id)
                                order_book_events = self._process_order_book_to_events(
                                    token_id,
                                    market_title,
                                    order_book,
                                    market_id=market_id,
                                    event_id=event_id,
                                    side='no' if idx == 1 else 'yes',
                                )
                                for ob_event in order_book_events:
                                    await self.event_queue.put(ob_event)

                            # Emit NewsEvent for new events only
                            yes_token_id = token_ids[0] if token_ids else ''
                            if yes_token_id:
                                yes_ticker = PolyMarketTicker(
                                    symbol=yes_token_id,
                                    name=market_title,
                                    token_id=yes_token_id,
                                    market_id=market_id,
                                    event_id=event_id,
                                )
                                news_content = f'{market_title}: {enriched_event.get("description", "")}'
                                news_event = NewsEvent(
                                    news=news_content,
                                    title=market_title,
                                    source='polymarket',
                                    event_id=event_id,
                                    ticker=yes_ticker,
                                )
                                await self.event_queue.put(news_event)

                        self.processed_event_ids.add(event_id)
                        with open(self.event_cache_file, 'a') as f:
                            f.write(json.dumps(enriched_event) + '\n')

                if self._first_poll:
                    self._first_poll = False
            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            await asyncio.sleep(self.polling_interval)

    def register_token_ticker(self, token_id: str, ticker: PolyMarketTicker) -> None:
        """Pre-register a ticker for a token_id so the refresh loop uses it."""
        self._known_tickers[token_id] = ticker

    def watch_token(self, token_id: str) -> None:
        """Mark a token as priority for order book refresh (e.g. when position opened)."""
        self._priority_tokens.add(token_id)

    def unwatch_token(self, token_id: str) -> None:
        """Remove a token from priority refresh (e.g. when position closed)."""
        self._priority_tokens.discard(token_id)

    def _evict_stale_tickers(self) -> None:
        """Evict non-priority tickers when _known_tickers exceeds max size.

        Removes the least-recently-refreshed non-priority tokens and cleans
        up their associated last_order_book_state entries.
        """
        if len(self._known_tickers) <= self._max_known_tickers:
            return

        # Collect non-priority tokens sorted by last refresh time (oldest first)
        evict_candidates = [
            (tid, self._last_refresh_time.get(tid, 0.0))
            for tid in self._known_tickers
            if tid not in self._priority_tokens
        ]
        evict_candidates.sort(key=lambda x: x[1])

        num_to_evict = len(self._known_tickers) - self._max_known_tickers
        for tid, _ in evict_candidates[:num_to_evict]:
            del self._known_tickers[tid]
            self._last_refresh_time.pop(tid, None)
            # Clean up last_order_book_state entries for evicted token
            stale_keys = [
                k for k in self.last_order_book_state if k.startswith(f'{tid}:')
            ]
            for k in stale_keys:
                del self.last_order_book_state[k]

    async def _process_refresh_result(
        self,
        token_id: str,
        ticker: PolyMarketTicker,
        order_book: Any,
    ) -> None:
        """Process a fetched order book: emit events for changed/stale levels."""
        # Use the latest ticker from _known_tickers (may have been enriched
        # by _poll_data since the refresh list was built).
        ticker = self._known_tickers.get(token_id, ticker)
        # --- Full snapshot: clear stale levels for this token ---
        old_bid_keys = {
            k
            for k in self.last_order_book_state
            if k.startswith(f'{token_id}:') and k.endswith(':bid')
        }
        old_ask_keys = {
            k
            for k in self.last_order_book_state
            if k.startswith(f'{token_id}:') and k.endswith(':ask')
        }

        seen_bid_keys: set[str] = set()
        for bid in order_book.bids:
            price = Decimal(bid.price)
            size = Decimal(bid.size)
            key = f'{token_id}:{bid.price}:bid'
            seen_bid_keys.add(key)
            prev_size = self.last_order_book_state.get(key, Decimal('0'))
            self.last_order_book_state[key] = size
            if size != prev_size:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size - prev_size,
                        side='bid',
                    )
                )

        seen_ask_keys: set[str] = set()
        for ask in order_book.asks:
            price = Decimal(ask.price)
            size = Decimal(ask.size)
            key = f'{token_id}:{ask.price}:ask'
            seen_ask_keys.add(key)
            prev_size = self.last_order_book_state.get(key, Decimal('0'))
            self.last_order_book_state[key] = size
            if size != prev_size:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size - prev_size,
                        side='ask',
                    )
                )

        # Remove levels that disappeared from the real order book
        for stale_key in old_bid_keys - seen_bid_keys:
            price_str = stale_key.split(':')[1]
            prev_size = self.last_order_book_state.pop(stale_key, Decimal('0'))
            if prev_size > 0:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=Decimal(price_str),
                        size=Decimal('0'),
                        size_delta=-prev_size,
                        side='bid',
                    )
                )
        for stale_key in old_ask_keys - seen_ask_keys:
            price_str = stale_key.split(':')[1]
            prev_size = self.last_order_book_state.pop(stale_key, Decimal('0'))
            if prev_size > 0:
                await self.event_queue.put(
                    OrderBookEvent(
                        ticker=ticker,
                        price=Decimal(price_str),
                        size=Decimal('0'),
                        size_delta=-prev_size,
                        side='ask',
                    )
                )

        # Derive PriceChangeEvent from best bid/ask mid-price
        best_bid = max(
            (Decimal(b.price) for b in order_book.bids),
            default=None,
        )
        best_ask = min(
            (Decimal(a.price) for a in order_book.asks),
            default=None,
        )
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        else:
            mid = None

        if mid is not None:
            prev_mid = self._last_mid_price.get(token_id)
            self._last_mid_price[token_id] = mid
            # Always emit for priority tokens (strategies need regular
            # heartbeats for warmup/calibration); only emit on change
            # for non-priority tokens to limit queue noise.
            if token_id in self._priority_tokens or prev_mid is None or mid != prev_mid:
                await self.event_queue.put(PriceChangeEvent(ticker=ticker, price=mid))

        self._last_refresh_time[token_id] = time.monotonic()

    async def _refresh_loop(self) -> None:
        """Periodically re-fetch order books for position tokens.

        Priority tokens (watched by strategies) are fetched first in a
        separate gather so that slow/dead non-priority tokens never block
        critical data flow.  Non-priority tokens are fetched afterwards
        in a rotating batch with a per-fetch timeout.
        """
        while True:
            await asyncio.sleep(self.orderbook_refresh_interval)

            # Evict stale tickers if we've exceeded the max
            self._evict_stale_tickers()

            # --- Priority tokens: fetch first, separately ---
            # Sort by least-recently-refreshed so all tokens get attention
            # over multiple cycles.  Cap batch size to avoid API rate limits.
            _PRIORITY_BATCH_SIZE = 20
            all_priority: list[tuple[str, PolyMarketTicker]] = []
            for tid in list(self._priority_tokens):
                ticker = self._known_tickers.get(tid)
                if ticker is None:
                    ticker = PolyMarketTicker(symbol=tid, name='', token_id=tid)
                all_priority.append((tid, ticker))
            all_priority.sort(key=lambda x: self._last_refresh_time.get(x[0], 0.0))
            priority_ids = all_priority[:_PRIORITY_BATCH_SIZE]

            async def _fetch_one(token_id: str, ticker: PolyMarketTicker):
                async with self._fetch_semaphore:
                    try:
                        order_book = await asyncio.wait_for(
                            self._fetch_order_book(token_id), timeout=10.0
                        )
                    except asyncio.TimeoutError:
                        return (token_id, ticker, None)
                    return (token_id, ticker, order_book)

            if priority_ids:
                results = await asyncio.gather(
                    *[_fetch_one(tid, t) for tid, t in priority_ids],
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    token_id, ticker, order_book = result
                    if not order_book:
                        continue
                    try:
                        await self._process_refresh_result(token_id, ticker, order_book)
                    except Exception:
                        logger.debug(
                            'Error processing refresh for %s',
                            token_id,
                            exc_info=True,
                        )

            # --- Non-priority rotating batch (with per-fetch timeout) ---
            non_priority = [
                (tid, t)
                for tid, t in self._known_tickers.items()
                if tid not in self._priority_tokens
            ]
            if non_priority:
                batch_size = 10
                start = self._refresh_offset % len(non_priority)
                batch = non_priority[start : start + batch_size]
                if len(batch) < batch_size:
                    batch.extend(non_priority[: batch_size - len(batch)])
                self._refresh_offset += batch_size

                async def _fetch_with_timeout(token_id: str, ticker: PolyMarketTicker):
                    async with self._fetch_semaphore:
                        try:
                            order_book = await asyncio.wait_for(
                                self._fetch_order_book(token_id), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            return (token_id, ticker, None)
                        return (token_id, ticker, order_book)

                results = await asyncio.gather(
                    *[_fetch_with_timeout(tid, t) for tid, t in batch],
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    token_id, ticker, order_book = result
                    if not order_book:
                        continue
                    try:
                        await self._process_refresh_result(token_id, ticker, order_book)
                    except Exception:
                        logger.debug(
                            'Error processing refresh for %s',
                            token_id,
                            exc_info=True,
                        )

            # Clean up last_order_book_state for tokens no longer tracked
            tracked_token_ids = set(self._known_tickers.keys())
            stale_state_keys = [
                k
                for k in self.last_order_book_state
                if k.split(':')[0] not in tracked_token_ids
            ]
            for k in stale_state_keys:
                del self.last_order_book_state[k]

    async def start(self) -> None:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(),
                timeout=8.0,
            )
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        for task in (self._poll_task, self._refresh_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._refresh_task = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None


class LiveNewsDataSource(DataSource):
    api_token: str
    cache_file: str
    polling_interval: float
    max_articles_per_poll: int
    languages: list[str]
    categories: list[str]
    base_url: str
    processed_article_ids: set[str]
    event_queue: asyncio.Queue

    def __init__(
        self,
        api_token: str,
        cache_file: str = 'news_cache.jsonl',
        polling_interval: float = 300.0,
        max_articles_per_poll: int = 10,
        languages: list[str] = None,
        categories: list[str] = None,
    ):
        self.api_token = api_token
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.languages = languages or ['en']
        self.categories = categories or []
        self.base_url = 'https://api.thenewsapi.com/v1/news/headlines'

        self.processed_article_ids = set()
        self.event_queue = asyncio.Queue()
        self._poll_task: asyncio.Task | None = None

        self._load_processed_articles()

    def _load_processed_articles(self) -> None:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    for line in f:
                        article = json.loads(line.strip())
                        if 'uuid' in article:
                            self.processed_article_ids.add(article['uuid'])
            except Exception as e:
                logger.warning('Error loading article cache: %s', e)

    async def _fetch_articles(self) -> list[dict[str, Any]]:  # noqa: C901
        try:
            today = datetime.now()

            params = {
                'api_token': self.api_token,
                'language': ','.join(self.languages),
                'published_on': today.strftime('%Y-%m-%d'),
                'locale': 'us',
                'limit': self.max_articles_per_poll,
            }

            if self.categories:
                params['categories'] = ','.join(self.categories)

            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(), timeout=30.0
            ) as client:
                response = await client.get(self.base_url, params=params)

                if response.status_code == 200:
                    try:
                        # Log raw response for debugging
                        logger.debug('Raw API response: %s...', response.text[:500])

                        # Try to parse the response as JSON
                        data = response.json()

                        # Verify that data is a dictionary
                        if not isinstance(data, dict):
                            logger.warning(
                                'API returned non-dictionary response: %s', type(data)
                            )
                            return []

                        # Check if 'data' key exists
                        if 'data' not in data:
                            logger.warning(
                                "API response missing 'data' key. Available keys: %s",
                                list(data.keys()),
                            )

                            # Try to extract articles from other possible structures
                            if 'articles' in data:
                                logger.debug("Found 'articles' key instead of 'data'")
                                articles = data.get('articles', [])
                                if isinstance(articles, list):
                                    return articles

                            # If this looks like it might be a single article
                            if 'title' in data or 'url' in data:
                                logger.debug('Response appears to be a single article')
                                return [data]

                            # Try other potential formats
                            for key, value in data.items():
                                if isinstance(value, list):
                                    logger.debug(
                                        "Found list under key '%s', trying this instead",
                                        key,
                                    )
                                    return value

                            return []

                        articles_data = data.get('data', [])

                        # Check if articles_data is a list
                        if isinstance(articles_data, list):
                            return articles_data

                        # If articles_data is a dict, it might be organized by categories
                        if isinstance(articles_data, dict):
                            logger.debug(
                                "'data' is a dictionary, attempting to flatten"
                            )
                            flattened_articles = []

                            for category, articles in articles_data.items():
                                if isinstance(articles, list):
                                    logger.debug(
                                        "Found %d articles in category '%s'",
                                        len(articles),
                                        category,
                                    )
                                    flattened_articles.extend(articles)
                                elif isinstance(articles, dict):
                                    logger.debug(
                                        "Category '%s' contains a dictionary, not a list",
                                        category,
                                    )
                                    flattened_articles.append(
                                        articles
                                    )  # Add as a single article

                            logger.debug(
                                'Flattened %d articles from categories',
                                len(flattened_articles),
                            )
                            return flattened_articles

                        logger.warning(
                            "Unexpected 'data' format: %s", type(articles_data)
                        )
                        return []

                    except json.JSONDecodeError as e:
                        logger.warning('Failed to parse API response as JSON: %s', e)
                        logger.debug('Raw response: %s...', response.text[:200])
                        return []
                else:
                    logger.warning('API error: HTTP %d', response.status_code)
                    logger.debug('Response: %s...', response.text[:200])
                    return []
        except Exception as e:
            logger.error('Error fetching articles: %s', e, exc_info=True)
            return []

    def _create_news_event(self, article: dict[str, Any]) -> NewsEvent:
        # Handle published_at date
        published_at = None
        if 'published_at' in article and article['published_at']:
            try:
                published_at_str = str(article['published_at'])
                if 'Z' in published_at_str:
                    published_at = datetime.fromisoformat(
                        published_at_str.replace('Z', '+00:00')
                    )
                else:
                    published_at = datetime.fromisoformat(published_at_str)
            except (ValueError, TypeError) as e:
                logger.warning(
                    "Error parsing date '%s': %s", article.get('published_at'), e
                )
                published_at = datetime.now()
        else:
            published_at = datetime.now()

        # Extract title safely
        title = ''
        if 'title' in article and article['title']:
            title = str(article['title'])

        # Extract description safely
        description = ''
        if 'description' in article and article['description']:
            description = str(article['description'])
        elif 'snippet' in article and article['snippet']:
            description = str(article['snippet'])

        # Create news content
        news_content = f'{title}: {description}' if description else title
        if not news_content:
            news_content = 'No content available'

        # Extract categories safely
        categories = []
        if 'categories' in article and isinstance(article['categories'], list):
            categories = article['categories']

        # Create the event
        return NewsEvent(
            news=news_content,
            title=title,
            source=str(article.get('source', '')),
            url=str(article.get('url', '')),
            published_at=published_at,
            categories=categories,
            description=description,
            image_url=str(article.get('image_url', '')),
            uuid=str(article.get('uuid', '')),
            event_id=str(article.get('uuid', '')),
        )

    async def _retry_on_error(
        self, func, *args, retries: int = 3, delay: int = 2, **kwargs
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.warning('Attempt %d failed: %s', attempt + 1, e)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    def _save_article(self, article: dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception as e:
            logger.error('Error saving article: %s', e)

    async def _poll_data(self) -> None:
        while True:
            try:
                logger.info('Fetching news articles...')
                articles = await self._retry_on_error(self._fetch_articles)
                logger.info('Received %d articles', len(articles))

                for article in articles:
                    try:
                        # Verify article is a dictionary
                        if not isinstance(article, dict):
                            logger.debug(
                                'Skipping non-dictionary article: %s', type(article)
                            )
                            continue

                        # Check for UUID
                        article_id = article.get('uuid')
                        if not article_id:
                            logger.debug('Article missing UUID, generating random ID')
                            article_id = str(uuid.uuid4())
                            article['uuid'] = article_id

                        # Skip processed articles
                        if article_id in self.processed_article_ids:
                            logger.debug(
                                'Skipping already processed article: %s', article_id
                            )
                            continue

                        # Create news event
                        event = self._create_news_event(article)

                        # Add to queue
                        await self.event_queue.put(event)
                        logger.debug(
                            'Added article to queue: %s - %s',
                            article_id,
                            article.get('title', 'No title'),
                        )

                        # Mark as processed and save
                        self.processed_article_ids.add(article_id)
                        self._save_article(article)

                    except Exception as e:
                        logger.error('Error processing article: %s', e, exc_info=True)
                        continue

            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            logger.debug('Sleeping for %s seconds', self.polling_interval)
            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def get_next_event(self) -> NewsEvent | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None


class LiveRSSNewsDataSource(DataSource):
    cache_file: str
    polling_interval: float
    max_articles_per_poll: int
    languages: list[str]
    categories: list[str]
    processed_article_ids: set[str]
    event_queue: asyncio.Queue

    RSS_FEEDS = {
        'https://feeds.content.dowjones.io/public/rss/RSSOpinion': ['opinion'],
        'https://feeds.content.dowjones.io/public/rss/RSSWorldNews': ['world'],
        'https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness': ['business'],
        'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain': ['finance'],
        'https://feeds.content.dowjones.io/public/rss/RSSWSJD': ['technology'],
        'https://feeds.content.dowjones.io/public/rss/RSSLifestyle': ['lifestyle'],
        'https://feeds.content.dowjones.io/public/rss/RSSUSnews': ['us'],
        'https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed': ['politics'],
        'https://feeds.content.dowjones.io/public/rss/socialeconomyfeed': ['economy'],
        'https://feeds.content.dowjones.io/public/rss/RSSArtsCulture': ['arts'],
        'https://feeds.content.dowjones.io/public/rss/latestnewsrealestate': [
            'real estate'
        ],
        'https://feeds.content.dowjones.io/public/rss/RSSPersonalFinance': [
            'personal finance'
        ],
        'https://feeds.content.dowjones.io/public/rss/socialhealth': ['health'],
        'https://feeds.content.dowjones.io/public/rss/RSSStyle': ['style'],
        'https://feeds.content.dowjones.io/public/rss/rsssportsfeed': ['sports'],
    }

    def __init__(
        self,
        cache_file: str = 'rss_news_cache.jsonl',
        polling_interval: float = 60.0,
        max_articles_per_poll: int = 50,
        categories: list[str] = None,
    ):
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.languages = ['en-us']
        self.categories = categories or []
        feedparser.CACHE_DIRECTORY = None
        feedparser._check_cache = lambda *args, **kwargs: None
        self.processed_article_ids: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._poll_task: asyncio.Task | None = None
        self._trim_cache()

    def _trim_cache(self) -> None:
        """Keep only the last 500 cached articles to avoid stale dedup."""
        if not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, encoding='utf-8') as f:
                lines = f.readlines()
            recent = lines[-500:] if len(lines) > 500 else lines
            for line in recent:
                article = json.loads(line.strip())
                if 'uuid' in article:
                    self.processed_article_ids.add(article['uuid'])
            # Rewrite with only recent entries
            if len(lines) > 500:
                with open(self.cache_file, 'w', encoding='utf-8') as f:
                    f.writelines(recent)
        except Exception as e:
            logger.error('Error trimming article cache: %s', e)

    def _save_article(self, article: dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception as e:
            logger.error('Error saving article: %s', e)

    def _extract_image_url(self, entry) -> str:
        if 'media_content' in entry:
            for media in entry.get('media_content', []):
                if isinstance(media, dict) and 'url' in media:
                    return media.get('url', '')
        if 'media_thumbnail' in entry:
            for media in entry.get('media_thumbnail', []):
                if isinstance(media, dict) and 'url' in media:
                    return media.get('url', '')
        return ''

    def _create_news_event(self, entry, feed_title, tags) -> NewsEvent:
        title = entry.get('title', 'No title')

        description = entry.get('description', '')

        if not description and 'summary' in entry:
            description = entry.get('summary', '')

        link = entry.get('link', '')

        guid = entry.get('guid', '')
        if isinstance(guid, dict):
            guid = guid.get('value', '')
        if not guid:
            guid = link
        if not guid:
            guid = str(uuid.uuid4())

        published_at = datetime.now(timezone.utc)
        if 'pubDate' in entry:
            try:
                pub_date_str = entry.get('pubDate', '')
                published_at = datetime.strptime(
                    pub_date_str, '%a, %d %b %Y %H:%M:%S %Z'
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError) as e:
                logger.debug("Error parsing date '%s': %s", entry.get('pubDate', ''), e)
        news_content = f'{title}: {description}' if description else title
        image_url = self._extract_image_url(entry)
        return NewsEvent(
            news=news_content,
            title=title,
            source=feed_title,
            url=link,
            published_at=published_at,
            categories=tags,
            description=description,
            image_url=image_url,
            uuid=guid,
            event_id=guid,
        )

    async def _retry_on_error(
        self, func, *args, retries: int = 3, delay: int = 2, **kwargs
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error('Attempt %d failed: %s', attempt + 1, e)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    async def _fetch_rss_feeds(self) -> list[dict[str, Any]]:  # noqa: C901
        results = []
        max_per_feed = max(self.max_articles_per_poll // len(self.RSS_FEEDS), 1)

        for feed_url, tags in self.RSS_FEEDS.items():
            try:
                logger.debug('Fetching %s', feed_url)
                feed = await asyncio.to_thread(feedparser.parse, feed_url)

                if not feed or not hasattr(feed, 'entries'):
                    logger.info('Feed from %s is invalid', feed_url)
                    continue

                feed_title = 'Unknown Source'
                if hasattr(feed, 'feed') and hasattr(feed.feed, 'title'):
                    feed_title = feed.feed.title

                if self.categories and not any(cat in tags for cat in self.categories):
                    continue

                feed_count = 0
                for entry in feed.entries:
                    if feed_count >= max_per_feed:
                        break

                    guid = entry.get('guid', '')
                    if isinstance(guid, dict):
                        guid = guid.get('value', '')
                    if not guid:
                        guid = entry.get('link', '')
                    if not guid:
                        guid = str(uuid.uuid4())

                    if guid in self.processed_article_ids:
                        continue

                    results.append((entry, feed_title, tags))
                    feed_count += 1

            except Exception as e:
                logger.error('Error fetching feed %s: %s', feed_url, e, exc_info=True)
        return results

    async def _poll_data(self) -> None:
        while True:
            try:
                news_items = await self._retry_on_error(self._fetch_rss_feeds)

                for entry, feed_title, tags in news_items:
                    try:
                        guid = entry.get('guid', '')
                        if isinstance(guid, dict):
                            guid = guid.get('value', '')
                        if not guid:
                            guid = entry.get('link', '')
                        if not guid:
                            guid = str(uuid.uuid4())

                        article = {
                            'uuid': guid,
                            'title': entry.get('title', 'No title'),
                            'description': entry.get('description', ''),
                            'link': entry.get('link', ''),
                            'pubDate': entry.get('pubDate', ''),
                            'source': feed_title,
                            'categories': tags,
                            'image_url': self._extract_image_url(entry),
                        }

                        event = self._create_news_event(entry, feed_title, tags)

                        await self.event_queue.put(event)

                        self.processed_article_ids.add(guid)
                        self._save_article(article)
                    except Exception as e:
                        logger.error('Error processing article: %s', e, exc_info=True)
                        continue

            except Exception as e:
                logger.error('Error in polling loop: %s', e, exc_info=True)

            logger.debug('Sleeping for %s seconds', self.polling_interval)
            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def get_next_event(self) -> NewsEvent | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
