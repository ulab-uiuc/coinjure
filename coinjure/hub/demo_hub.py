"""DemoHub — synthetic price generator using the same protocol as MarketDataHub.

Generates random PriceChangeEvent + OrderBookEvent for subscribed tokens so
strategies can be exercised without live exchange data.

Usage:
    coinjure hub start --demo --detach
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from coinjure.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.market.relations import RelationStore
from coinjure.ticker import PolyMarketTicker

logger = logging.getLogger(__name__)

_BOOK_SIZE = Decimal('1000')
_HALF_SPREAD = Decimal('0.02')


class DemoHub:
    """Synthetic data hub — same Unix socket protocol as MarketDataHub."""

    def __init__(
        self,
        socket_path: Path,
        interval: float = 0.5,
    ) -> None:
        self.socket_path = socket_path
        self._interval = interval

        # Subscriber state (same layout as MarketDataHub)
        self._subscribers: dict[int, asyncio.Queue[bytes]] = {}
        self._sub_filters: dict[int, set[str]] = {}
        self._sub_writers: dict[int, asyncio.StreamWriter] = {}
        self._sub_tasks: set[asyncio.Task] = set()
        self._next_id = 0

        # token_symbol → (Ticker, current_price)
        self._tokens: dict[str, tuple[PolyMarketTicker, float]] = {}
        # yes_token_id → no_token_id mapping
        self._yes_to_no: dict[str, str] = {}

        # Relation-aware price groups:
        # list of (spread_type, [yes_token_id, ...]) — tokens that move together
        self._relation_groups: list[tuple[str, list[str]]] = []
        # Tokens that belong to a relation group (skip in independent walk)
        self._grouped_tokens: set[str] = set()

        self._server: asyncio.AbstractServer | None = None
        self._gen_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._start_time: float = 0.0
        self._events_total: int = 0
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._start_time = time.monotonic()
        self._running = True

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._seed_tokens_from_relations()

        # Start real news source (RSS + Polymarket crawl)
        self._news_source = self._build_news_source()
        if self._news_source:
            await self._news_source.start()

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        logger.info(
            'DemoHub listening on %s (%d tokens)', self.socket_path, len(self._tokens)
        )

        self._gen_task = asyncio.create_task(self._generate_loop())
        self._news_task = asyncio.create_task(self._news_relay_loop())

        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            for task in list(self._sub_tasks):
                task.cancel()
            if self._sub_tasks:
                await asyncio.gather(*self._sub_tasks, return_exceptions=True)
            if self._gen_task:
                self._gen_task.cancel()
            if self._news_task:
                self._news_task.cancel()
            if self._news_source:
                await self._news_source.stop()
            if self._server:
                self._server.close()
                await self._server.wait_closed()
            if self.socket_path.exists():
                self.socket_path.unlink()

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # Token seeding
    # ------------------------------------------------------------------

    def _seed_tokens_from_relations(self) -> None:
        """Pre-load tickers from all backtest_passed relations."""
        store = RelationStore()
        for rel in store.list():
            if rel.status != 'backtest_passed':
                continue
            group_tids: list[str] = []
            for m in rel.markets:
                mid = (
                    m.get('condition_id', '')
                    or m.get('market_ticker', '')
                    or m.get('ticker', '')
                    or m.get('id', '')
                )
                tid = m.get('token_id', '')
                token_ids = m.get('token_ids', [])
                if not tid:
                    tid = token_ids[0] if token_ids else ''
                if not tid:
                    continue
                no_tid = token_ids[1] if len(token_ids) > 1 else ''
                name = m.get('question', m.get('title', ''))[:60]
                # YES token
                yes_ticker = PolyMarketTicker(
                    symbol=tid,
                    name=name,
                    token_id=tid,
                    market_id=mid,
                    side='yes',
                )
                bid = float(m.get('best_bid', 0) or 0)
                ask = float(m.get('best_ask', 0) or 0)
                base = (bid + ask) / 2 if (bid and ask) else 0.5
                base = max(0.05, min(0.95, base))
                self._tokens[tid] = (yes_ticker, base)
                group_tids.append(tid)
                # NO token (complement price)
                if no_tid:
                    no_ticker = PolyMarketTicker(
                        symbol=no_tid,
                        name=name,
                        token_id=no_tid,
                        market_id=mid,
                        side='no',
                    )
                    self._tokens[no_tid] = (no_ticker, 1.0 - base)
                    self._yes_to_no[tid] = no_tid

            # Build relation group for correlated price generation
            if len(group_tids) >= 2:
                self._relation_groups.append((rel.spread_type, group_tids))
                self._grouped_tokens.update(group_tids)

        logger.info(
            'DemoHub seeded %d tokens from relations (%d groups)',
            len(self._tokens),
            len(self._relation_groups),
        )

    # ------------------------------------------------------------------
    # Real news relay
    # ------------------------------------------------------------------

    @staticmethod
    def _build_news_source():
        """Create a composite news source (RSS + Polymarket crawl)."""
        try:
            from coinjure.data.live.polymarket import LiveRSSNewsDataSource

            return LiveRSSNewsDataSource(polling_interval=60.0)
        except Exception:
            logger.warning('Failed to create news source', exc_info=True)
            return None

    async def _news_relay_loop(self) -> None:
        """Pull real NewsEvents from RSS and fan out to subscribers."""
        if not self._news_source:
            return
        while self._running:
            try:
                event = await self._news_source.get_next_event()
                if event is None:
                    continue
                if isinstance(event, NewsEvent):
                    self._fan_one(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug('News relay error', exc_info=True)
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Synthetic event generation
    # ------------------------------------------------------------------

    async def _generate_loop(self) -> None:
        """Generate relation-aware PriceChangeEvent + synthetic orderbook.

        Tokens in a relation group move together with small individual noise,
        and occasionally inject constraint violations to create arb opportunities.
        Ungrouped tokens still use independent random walks.
        """
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._subscribers:
                continue

            # Collect watched symbols from all subscribers
            watched: set[str] = set()
            for filt in self._sub_filters.values():
                watched |= filt

            now = datetime.now(timezone.utc)
            # Track tokens already stepped this tick to avoid overwriting
            # violations injected by earlier groups.
            stepped: set[str] = set()

            # ── Step 1: relation-aware grouped tokens ──
            for spread_type, tids in self._relation_groups:
                if spread_type in ('complementary', 'exclusivity'):
                    self._step_group(tids, watched, now)
                    stepped.update(tids)
                elif spread_type == 'implication':
                    self._step_implication(tids, watched, now, stepped)
                else:
                    # correlated, conditional, temporal, etc. — shared driver + noise
                    self._step_correlated(tids, watched, now)
                    stepped.update(tids)

            # ── Step 2: ungrouped tokens — independent random walk ──
            for sym, (ticker, price) in list(self._tokens.items()):
                if sym in self._grouped_tokens:
                    continue
                if watched and sym not in watched:
                    continue
                if ticker.side == 'no':
                    continue
                step = random.gauss(0, 0.02)
                price = max(0.01, min(0.99, price + step))
                self._tokens[sym] = (ticker, price)
                self._emit_price(sym, ticker, price, now)

    def _step_group(self, tids: list[str], watched: set[str], now: datetime) -> None:
        """Complementary/exclusivity: prices sum to ~1.0 with occasional violations.

        A shared sentiment shock redistributes weight among outcomes.
        ~10% of steps inject a small sum deviation to create arb opportunities.
        """
        prices = []
        for tid in tids:
            if tid not in self._tokens:
                return
            prices.append(self._tokens[tid][1])
        if not prices:
            return

        total = sum(prices)
        if total <= 0:
            return

        # Normalize to current sum, then apply small per-outcome noise
        weights = [p / total for p in prices]
        new_weights = []
        for w in weights:
            noise = random.gauss(0, 0.015)
            new_weights.append(max(0.001, w + noise))

        # ~25% chance: inject sum violation (±0.04-0.10) for arb opportunity
        target_sum = 1.0
        if random.random() < 0.25:
            target_sum = 1.0 + random.choice([-1, 1]) * random.uniform(0.04, 0.10)

        # Rescale weights to target sum
        w_total = sum(new_weights)
        new_prices = [w / w_total * target_sum for w in new_weights]

        for i, tid in enumerate(tids):
            if watched and tid not in watched:
                continue
            ticker, _ = self._tokens[tid]
            if ticker.side == 'no':
                continue
            p = max(0.01, min(0.99, new_prices[i]))
            self._tokens[tid] = (ticker, p)
            self._emit_price(tid, ticker, p, now)

    def _step_implication(
        self,
        tids: list[str],
        watched: set[str],
        now: datetime,
        stepped: set[str] | None = None,
    ) -> None:
        """Implication pair: A ≤ B (earlier date ≤ later date).

        For each 2-token pair: apply a shared step + small individual noise,
        then ~15% of the time inject a violation where A's price > B's price.
        Tokens already stepped by a prior group are skipped (only emit, no step).
        """
        if stepped is None:
            stepped = set()

        # Only handle pairs (implication relations are always 2 markets)
        if len(tids) != 2:
            return
        tid_a, tid_b = tids[0], tids[1]
        if tid_a not in self._tokens or tid_b not in self._tokens:
            return

        _, price_a = self._tokens[tid_a]
        _, price_b = self._tokens[tid_b]

        # Apply shared step + individual noise (only for tokens not yet stepped)
        shared_step = random.gauss(0, 0.015)

        if tid_a not in stepped:
            price_a = max(
                0.01, min(0.99, price_a + shared_step + random.gauss(0, 0.015))
            )
        if tid_b not in stepped:
            price_b = max(
                0.01, min(0.99, price_b + shared_step + random.gauss(0, 0.015))
            )

        # ~30% chance: inject violation — push A above B by 0.03-0.08
        if random.random() < 0.30:
            mid = (price_a + price_b) / 2
            boost = random.uniform(0.03, 0.08)
            price_a = min(0.99, mid + boost)
            price_b = max(0.01, mid - boost)

        # Update and emit
        for tid, p in [(tid_a, price_a), (tid_b, price_b)]:
            if watched and tid not in watched:
                continue
            ticker, _ = self._tokens[tid]
            if ticker.side == 'no':
                continue
            self._tokens[tid] = (ticker, p)
            self._emit_price(tid, ticker, p, now)
            stepped.add(tid)

    def _step_correlated(
        self, tids: list[str], watched: set[str], now: datetime
    ) -> None:
        """Correlated/conditional/temporal: shared driver + individual noise.

        Prices move together (correlation ~0.7) with individual deviations.
        """
        shared_step = random.gauss(0, 0.015)
        for tid in tids:
            if tid not in self._tokens:
                continue
            ticker, price = self._tokens[tid]
            if ticker.side == 'no':
                continue
            if watched and tid not in watched:
                continue
            individual = random.gauss(0, 0.02)
            p = max(0.01, min(0.99, price + shared_step + individual))
            self._tokens[tid] = (ticker, p)
            self._emit_price(tid, ticker, p, now)

    def _emit_price(
        self, sym: str, ticker: PolyMarketTicker, price: float, now: datetime
    ) -> None:
        """Emit PriceChange + OrderBook events for a YES token and its NO complement."""
        price_dec = Decimal(f'{price:.4f}')

        # YES side
        self._fan_one(PriceChangeEvent(ticker=ticker, price=price_dec, timestamp=now))
        bid_p = max(price_dec - _HALF_SPREAD, Decimal('0.001'))
        ask_p = min(price_dec + _HALF_SPREAD, Decimal('0.999'))
        self._fan_one(
            OrderBookEvent(
                ticker=ticker,
                price=bid_p,
                size=_BOOK_SIZE,
                size_delta=_BOOK_SIZE,
                side='bid',
            )
        )
        self._fan_one(
            OrderBookEvent(
                ticker=ticker,
                price=ask_p,
                size=_BOOK_SIZE,
                size_delta=_BOOK_SIZE,
                side='ask',
            )
        )

        # NO side (complement)
        no_sym = self._yes_to_no.get(sym)
        if no_sym and no_sym in self._tokens:
            no_ticker, _ = self._tokens[no_sym]
            no_price = Decimal('1') - price_dec
            self._tokens[no_sym] = (no_ticker, float(no_price))
            self._fan_one(
                PriceChangeEvent(ticker=no_ticker, price=no_price, timestamp=now)
            )
            no_bid = max(no_price - _HALF_SPREAD, Decimal('0.001'))
            no_ask = min(no_price + _HALF_SPREAD, Decimal('0.999'))
            self._fan_one(
                OrderBookEvent(
                    ticker=no_ticker,
                    price=no_bid,
                    size=_BOOK_SIZE,
                    size_delta=_BOOK_SIZE,
                    side='bid',
                )
            )
            self._fan_one(
                OrderBookEvent(
                    ticker=no_ticker,
                    price=no_ask,
                    size=_BOOK_SIZE,
                    size_delta=_BOOK_SIZE,
                    side='ask',
                )
            )

    # ------------------------------------------------------------------
    # Fan-out (same as MarketDataHub)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ticker_key(event: Event) -> str | None:
        ticker = getattr(event, 'ticker', None)
        return ticker.symbol if ticker else None

    def _fan_one(self, event: Event) -> None:
        line = self._serialize_event(event)
        if line is None:
            return
        self._events_total += 1
        ticker_key = self._extract_ticker_key(event)
        encoded = (line + '\n').encode()
        dead: list[int] = []
        for sub_id, q in list(self._subscribers.items()):
            sub_filter = self._sub_filters.get(sub_id)
            if sub_filter and ticker_key is not None and ticker_key not in sub_filter:
                continue
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(encoded)
            except Exception:
                dead.append(sub_id)
        for sub_id in dead:
            self._subscribers.pop(sub_id, None)
            self._sub_filters.pop(sub_id, None)
            self._sub_writers.pop(sub_id, None)

    def _serialize_event(self, event: Event) -> str | None:
        ticker = getattr(event, 'ticker', None)

        # NewsEvent may have no ticker
        if isinstance(event, NewsEvent):
            ticker_data: dict[str, Any] = {}
            ticker_type = 'none'
            if isinstance(ticker, PolyMarketTicker):
                ticker_data = {
                    'symbol': ticker.symbol,
                    'name': ticker.name,
                    'token_id': ticker.token_id,
                    'market_id': ticker.market_id,
                    'event_id': getattr(ticker, 'event_id', ''),
                    'side': ticker.side,
                }
                ticker_type = 'polymarket'
            payload: dict[str, Any] = {
                'ticker_type': ticker_type,
                'ticker': ticker_data,
                'type': 'NewsEvent',
                'news': event.news,
                'title': event.title,
                'source': getattr(event, 'source', ''),
                'url': getattr(event, 'url', ''),
                'event_id': getattr(event, 'event_id', ''),
            }
            pa = getattr(event, 'published_at', None)
            if pa:
                payload['published_at'] = pa.isoformat()
            return json.dumps(payload, separators=(',', ':'))

        if ticker is None or not isinstance(ticker, PolyMarketTicker):
            return None
        ticker_data = {
            'symbol': ticker.symbol,
            'name': ticker.name,
            'token_id': ticker.token_id,
            'market_id': ticker.market_id,
            'event_id': getattr(ticker, 'event_id', ''),
            'side': ticker.side,
        }
        payload = {'ticker_type': 'polymarket', 'ticker': ticker_data}
        if isinstance(event, PriceChangeEvent):
            payload['type'] = 'PriceChangeEvent'
            payload['price'] = str(event.price)
            ts = event.timestamp or datetime.now(timezone.utc)
            payload['timestamp'] = ts.isoformat()
        elif isinstance(event, OrderBookEvent):
            payload['type'] = 'OrderBookEvent'
            payload['price'] = str(event.price)
            payload['size'] = str(event.size)
            payload['size_delta'] = str(event.size_delta)
            payload['side'] = event.side
        else:
            return None
        return json.dumps(payload, separators=(',', ':'))

    # ------------------------------------------------------------------
    # Connection handler (same protocol as MarketDataHub)
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._sub_tasks.add(task)
        try:
            try:
                first_line = await asyncio.wait_for(reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                first_line = b''

            if first_line.strip():
                try:
                    msg = json.loads(first_line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    writer.close()
                    return
                cmd = msg.get('cmd', '')
                if cmd == 'subscribe':
                    await self._handle_subscribe(msg, reader, writer)
                else:
                    await self._handle_control(msg, writer)
            else:
                await self._handle_subscribe({}, reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if task:
                self._sub_tasks.discard(task)

    async def _handle_subscribe(
        self, msg: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        tickers = msg.get('tickers', [])
        ticker_filter = set(tickers)
        sub_id = self._next_id
        self._next_id += 1

        resp = json.dumps({'ok': True, 'sub_id': sub_id}) + '\n'
        writer.write(resp.encode())
        await writer.drain()

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._subscribers[sub_id] = q
        self._sub_filters[sub_id] = ticker_filter
        self._sub_writers[sub_id] = writer
        logger.info(
            'DemoHub: subscriber %d connected (%d tickers)', sub_id, len(ticker_filter)
        )

        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.pop(sub_id, None)
            self._sub_filters.pop(sub_id, None)
            self._sub_writers.pop(sub_id, None)
            logger.info('DemoHub: subscriber %d disconnected', sub_id)

    async def _handle_control(self, req: dict, writer: asyncio.StreamWriter) -> None:
        cmd = req.get('cmd', '')
        if cmd == 'status':
            uptime = time.monotonic() - self._start_time
            resp: dict[str, Any] = {
                'ok': True,
                'mode': 'demo',
                'subscribers': len(self._subscribers),
                'events_total': self._events_total,
                'tokens': len(self._tokens),
                'uptime_s': round(uptime, 1),
            }
        elif cmd == 'stop':
            resp = {'ok': True, 'status': 'stopping'}
            writer.write((json.dumps(resp) + '\n').encode())
            await writer.drain()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self.stop())
            )
            return
        elif cmd == 'watch_token':
            token_id = req.get('token_id', '')
            sub_id = req.get('sub_id')
            if token_id and sub_id is not None and sub_id in self._sub_filters:
                self._sub_filters[sub_id].add(token_id)
            resp = {'ok': True}
        elif cmd == 'unwatch_token':
            token_id = req.get('token_id', '')
            sub_id = req.get('sub_id')
            if token_id and sub_id is not None and sub_id in self._sub_filters:
                self._sub_filters[sub_id].discard(token_id)
            resp = {'ok': True}
        else:
            resp = {'ok': False, 'error': f'Unknown command: {cmd!r}'}

        writer.write((json.dumps(resp) + '\n').encode())
        await writer.drain()
