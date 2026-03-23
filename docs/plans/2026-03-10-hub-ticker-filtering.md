# Hub Ticker Filtering Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Hub-side per-subscriber ticker filtering so each subscriber only receives events for tickers it cares about, reducing fan-out from O(events × subscribers) to O(events × ~2-3).

**Architecture:** Change subscriber protocol so the `HubDataSource` sends a `{"cmd": "subscribe", "tickers": [...]}` message on connect. Hub stores a `set[str]` of ticker symbols per subscriber and only enqueues events whose ticker symbol is in that set. Existing `watch_token`/`unwatch_token` control commands also update the subscriber's filter set (identified by a subscriber ID returned on subscribe). Subscribers with no filter (empty set) receive nothing — there's no reason for a subscriber to want all events.

**Tech Stack:** Python asyncio, Unix sockets, JSON protocol

---

### Task 1: Hub — per-subscriber filter storage

**Files:**

- Modify: `coinjure/hub/hub.py`
- Test: `tests/test_hub_ticker_filter.py`

**Step 1: Write the failing tests**

Create `tests/test_hub_ticker_filter.py`:

```python
"""Tests for hub-side per-subscriber ticker filtering."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coinjure.hub.hub import MarketDataHub
from coinjure.events import OrderBookEvent, PriceChangeEvent
from coinjure.ticker import PolyMarketTicker, KalshiTicker


def _poly_ticker(token_id: str = 'tok_A') -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol=token_id, name='Test', token_id=token_id,
        market_id='m1', event_id='e1', side='yes',
    )


def _kalshi_ticker(market_ticker: str = 'KXFOO') -> KalshiTicker:
    return KalshiTicker(
        symbol=market_ticker, name='Test', market_ticker=market_ticker,
        event_ticker='KXFOO', series_ticker='KXFOO', side='yes',
    )


class TestFanLoopFiltering:
    """_fan_loop should only enqueue events matching a subscriber's ticker filter."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    def test_subscriber_with_matching_filter_receives_event(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = {'tok_A'}

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'), size=Decimal('100'),
            size_delta=Decimal('100'), side='bid',
        )
        hub._fan_one(event)
        assert not q.empty()

    def test_subscriber_with_non_matching_filter_skips_event(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = {'tok_B'}

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'), size=Decimal('100'),
            size_delta=Decimal('100'), side='bid',
        )
        hub._fan_one(event)
        assert q.empty()

    def test_subscriber_with_empty_filter_receives_nothing(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = set()

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'), size=Decimal('100'),
            size_delta=Decimal('100'), side='bid',
        )
        hub._fan_one(event)
        assert q.empty()

    def test_kalshi_ticker_filtered_by_symbol(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = {'KXFOO'}

        event = PriceChangeEvent(
            ticker=_kalshi_ticker('KXFOO'),
            price=Decimal('0.7'),
        )
        hub._fan_one(event)
        assert not q.empty()

    def test_multiple_subscribers_different_filters(self, hub: MarketDataHub) -> None:
        q1: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        q2: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        hub._subscribers[0] = q1
        hub._subscribers[1] = q2
        hub._sub_filters[0] = {'tok_A'}
        hub._sub_filters[1] = {'tok_B'}

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'), size=Decimal('100'),
            size_delta=Decimal('100'), side='bid',
        )
        hub._fan_one(event)
        assert not q1.empty()
        assert q2.empty()
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py -v`
Expected: FAIL — `_sub_filters` and `_fan_one` don't exist yet.

**Step 3: Implement hub filter storage and `_fan_one`**

In `coinjure/hub/hub.py`, make these changes:

1. Add `_sub_filters: dict[int, set[str]]` in `__init__`:

```python
# In __init__, after self._subscribers line:
self._sub_filters: dict[int, set[str]] = {}
```

2. Add `_extract_ticker_key` static method:

```python
@staticmethod
def _extract_ticker_key(event: Event) -> str | None:
    """Extract the filter key from an event's ticker.

    For Polymarket: ticker.symbol (which is the token_id).
    For Kalshi: ticker.symbol (which is the market_ticker or market_ticker:side).
    """
    ticker = getattr(event, 'ticker', None)
    if ticker is None:
        return None
    return ticker.symbol
```

3. Extract the broadcast logic from `_fan_loop` into `_fan_one` and add filtering:

```python
def _fan_one(self, event: Event) -> None:
    """Broadcast a single event to subscribers whose filter matches."""
    line = self._serialize_event(event)
    if line is None:
        return
    self._events_total += 1
    ticker_key = self._extract_ticker_key(event)
    encoded = (line + '\n').encode()
    dead: list[int] = []
    for sub_id, q in list(self._subscribers.items()):
        # Check filter: subscriber must have this ticker in their set
        sub_filter = self._sub_filters.get(sub_id)
        if sub_filter is not None and ticker_key not in sub_filter:
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
```

4. Update `_fan_loop` to call `_fan_one`:

```python
async def _fan_loop(self) -> None:
    """Pull events from source and broadcast to filtered subscriber queues."""
    while self._running:
        try:
            event = await self._source.get_next_event()
            if event is None:
                continue
            if not isinstance(event, (OrderBookEvent, PriceChangeEvent)):
                continue
            self._fan_one(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Hub fan loop error', exc_info=True)
            await asyncio.sleep(1.0)
```

5. Clean up `_sub_filters` in `_handle_subscriber` finally block:

```python
# In _handle_subscriber, in the finally block, add:
self._sub_filters.pop(sub_id, None)
```

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add coinjure/hub/hub.py tests/test_hub_ticker_filter.py
git commit -m "feat(hub): add per-subscriber ticker filter storage and _fan_one"
```

---

### Task 2: Hub — subscribe protocol

**Files:**

- Modify: `coinjure/hub/hub.py`
- Modify: `tests/test_hub_ticker_filter.py`

**Step 1: Write the failing tests**

Append to `tests/test_hub_ticker_filter.py`:

```python
class TestSubscribeProtocol:
    """Subscriber sends {"cmd": "subscribe", "tickers": [...]} on connect.
    Hub registers filter and starts streaming."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    @pytest.mark.asyncio
    async def test_subscribe_registers_filter(self, hub: MarketDataHub) -> None:
        raw = json.dumps({'cmd': 'subscribe', 'tickers': ['tok_A', 'tok_B']}).encode() + b'\n'
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        # After subscribe message, simulate hub closing
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = asyncio.coroutine(lambda: None)
        writer.close = MagicMock()
        writer.wait_closed = asyncio.coroutine(lambda: None)

        await hub._handle_connection(reader, writer)

        # The subscribe response should have been written
        assert writer.write.called
        first_write = writer.write.call_args_list[0][0][0]
        resp = json.loads(first_write.decode().strip())
        assert resp['ok'] is True
        assert 'sub_id' in resp

    @pytest.mark.asyncio
    async def test_subscribe_with_empty_tickers(self, hub: MarketDataHub) -> None:
        raw = json.dumps({'cmd': 'subscribe', 'tickers': []}).encode() + b'\n'
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = asyncio.coroutine(lambda: None)
        writer.close = MagicMock()
        writer.wait_closed = asyncio.coroutine(lambda: None)

        await hub._handle_connection(reader, writer)

        first_write = writer.write.call_args_list[0][0][0]
        resp = json.loads(first_write.decode().strip())
        assert resp['ok'] is True

    @pytest.mark.asyncio
    async def test_legacy_subscriber_no_initial_data_gets_no_filter(self, hub: MarketDataHub) -> None:
        """Legacy subscribers (no initial message) should get None filter (receive nothing)."""
        reader = asyncio.StreamReader()
        # Feed nothing — triggers 0.1s timeout → treated as legacy subscriber
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = asyncio.coroutine(lambda: None)
        writer.close = MagicMock()
        writer.wait_closed = asyncio.coroutine(lambda: None)

        await hub._handle_connection(reader, writer)
        # Legacy subscriber should still be registered but with empty filter
        # (they'll receive nothing until they watch_token)
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py::TestSubscribeProtocol -v`
Expected: FAIL — `subscribe` command not handled yet.

**Step 3: Implement subscribe protocol in hub**

In `coinjure/hub/hub.py`:

1. Change `_handle_connection` to detect `subscribe` command vs control vs legacy:

```python
async def _handle_connection(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    task = asyncio.current_task()
    if task:
        self._sub_tasks.add(task)
    try:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=0.1)
        except asyncio.TimeoutError:
            raw = b''

        if raw.strip():
            try:
                msg = json.loads(raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = None

            if isinstance(msg, dict) and msg.get('cmd') == 'subscribe':
                await self._handle_subscribe(msg, reader, writer)
            elif raw.strip():
                await self._handle_control(raw, writer)
        else:
            # Legacy subscriber — register with empty filter
            await self._handle_subscriber(reader, writer, ticker_filter=set())
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug('Hub connection error', exc_info=True)
    finally:
        if task:
            self._sub_tasks.discard(task)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
```

2. Add `_handle_subscribe` method:

```python
async def _handle_subscribe(
    self,
    msg: dict[str, Any],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle subscribe command: register filter, send ack, then stream events."""
    tickers = msg.get('tickers', [])
    ticker_filter = set(tickers)

    # Allocate subscriber ID early so we can return it
    sub_id = self._next_id
    self._next_id += 1

    # Send ack
    resp = json.dumps({'ok': True, 'sub_id': sub_id}) + '\n'
    writer.write(resp.encode())
    await writer.drain()

    # Register and start streaming
    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
    self._subscribers[sub_id] = q
    self._sub_filters[sub_id] = ticker_filter
    logger.debug(
        'Hub: subscriber %d connected with %d ticker filter(s) (total=%d)',
        sub_id, len(ticker_filter), len(self._subscribers),
    )
    try:
        while self._running:
            data = await q.get()
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        self._subscribers.pop(sub_id, None)
        self._sub_filters.pop(sub_id, None)
        logger.debug(
            'Hub: subscriber %d disconnected (total=%d)',
            sub_id, len(self._subscribers),
        )
```

3. Update `_handle_subscriber` to accept `ticker_filter` param:

```python
async def _handle_subscriber(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    *, ticker_filter: set[str] | None = None,
) -> None:
    sub_id = self._next_id
    self._next_id += 1
    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
    self._subscribers[sub_id] = q
    self._sub_filters[sub_id] = ticker_filter if ticker_filter is not None else set()
    # ... rest unchanged ...
```

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add coinjure/hub/hub.py tests/test_hub_ticker_filter.py
git commit -m "feat(hub): implement subscribe protocol with ticker filter"
```

---

### Task 3: Hub — update filter via watch_token/unwatch_token control commands

**Files:**

- Modify: `coinjure/hub/hub.py`
- Modify: `tests/test_hub_ticker_filter.py`

**Step 1: Write the failing tests**

Append to `tests/test_hub_ticker_filter.py`:

```python
class TestFilterUpdateViaControl:
    """watch_token/unwatch_token should update subscriber filters too."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        source.watch_token = MagicMock()
        source.unwatch_token = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    def test_watch_token_updates_subscriber_filter(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        hub._subscribers[0] = q
        hub._sub_filters[0] = {'tok_A'}

        hub._update_all_filters('tok_B', add=True)

        assert 'tok_B' in hub._sub_filters[0]
        assert 'tok_A' in hub._sub_filters[0]

    def test_unwatch_token_updates_subscriber_filter(self, hub: MarketDataHub) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        hub._subscribers[0] = q
        hub._sub_filters[0] = {'tok_A', 'tok_B'}

        hub._update_all_filters('tok_A', add=False)

        assert 'tok_A' not in hub._sub_filters[0]
        assert 'tok_B' in hub._sub_filters[0]
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py::TestFilterUpdateViaControl -v`
Expected: FAIL — `_update_all_filters` doesn't exist.

**Step 3: Implement**

Actually, on reflection this is wrong. `watch_token` is a global command to tell the **data source** to prioritize fetching a token. It's NOT per-subscriber. The subscriber filter is per-subscriber and set via the subscribe protocol.

The right approach: `watch_token` control command should accept an optional `sub_id` param to update a specific subscriber's filter. This way, HubDataSource can send `watch_token` with its `sub_id` to add tickers to its own filter dynamically.

In `_handle_control`, update the `watch_token` handler:

```python
elif cmd == 'watch_token':
    token_id = req.get('token_id', '')
    if token_id:
        # Update data source priority
        watch = getattr(self._source, 'watch_token', None)
        if watch:
            watch(token_id)
        # Update specific subscriber's filter if sub_id provided
        sub_id = req.get('sub_id')
        if sub_id is not None and sub_id in self._sub_filters:
            self._sub_filters[sub_id].add(token_id)
        resp = {'ok': True}
    else:
        resp = {'ok': False, 'error': 'token_id required'}
elif cmd == 'unwatch_token':
    token_id = req.get('token_id', '')
    if token_id:
        unwatch = getattr(self._source, 'unwatch_token', None)
        if unwatch:
            unwatch(token_id)
        sub_id = req.get('sub_id')
        if sub_id is not None and sub_id in self._sub_filters:
            self._sub_filters[sub_id].discard(token_id)
        resp = {'ok': True}
    else:
        resp = {'ok': False, 'error': 'token_id required'}
```

Update tests accordingly to use `sub_id` in the control command.

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add coinjure/hub/hub.py tests/test_hub_ticker_filter.py
git commit -m "feat(hub): watch_token/unwatch_token update subscriber filter by sub_id"
```

---

### Task 4: HubDataSource — send subscribe on connect, track sub_id

**Files:**

- Modify: `coinjure/hub/subscriber.py`
- Modify: `tests/test_hub_ticker_filter.py`

**Step 1: Write the failing tests**

Append to `tests/test_hub_ticker_filter.py`:

```python
class TestHubDataSourceSubscribe:
    """HubDataSource should send subscribe with tickers on connect."""

    def test_init_with_tickers(self, tmp_path: Path) -> None:
        src = HubDataSource(tmp_path / 'hub.sock', tickers=['tok_A', 'tok_B'])
        assert src._tickers == {'tok_A', 'tok_B'}

    def test_init_without_tickers(self, tmp_path: Path) -> None:
        src = HubDataSource(tmp_path / 'hub.sock')
        assert src._tickers == set()

    def test_watch_token_adds_to_local_set(self, tmp_path: Path) -> None:
        src = HubDataSource(tmp_path / 'hub.sock', tickers=['tok_A'])
        src.watch_token('tok_B')
        assert 'tok_B' in src._tickers

    def test_unwatch_token_removes_from_local_set(self, tmp_path: Path) -> None:
        src = HubDataSource(tmp_path / 'hub.sock', tickers=['tok_A', 'tok_B'])
        src.unwatch_token('tok_A')
        assert 'tok_A' not in src._tickers
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py::TestHubDataSourceSubscribe -v`
Expected: FAIL — `tickers` param doesn't exist, `_tickers` doesn't exist.

**Step 3: Implement HubDataSource changes**

In `coinjure/hub/subscriber.py`:

1. Add `tickers` param to `__init__`:

```python
def __init__(self, socket_path: Path, queue_size: int = 1000,
             tickers: list[str] | None = None) -> None:
    self.socket_path = socket_path
    self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
    self._reader_task: asyncio.Task | None = None
    self._running: bool = False
    self._tickers: set[str] = set(tickers) if tickers else set()
    self._sub_id: int | None = None
```

2. Update `_connect_loop` → `_read_events` to send subscribe message first:

```python
async def _read_events(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        # Send subscribe message with current ticker filter
        subscribe_msg = json.dumps({
            'cmd': 'subscribe',
            'tickers': list(self._tickers),
        }) + '\n'
        writer.write(subscribe_msg.encode())
        await writer.drain()

        # Read ack
        ack_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if ack_line:
            try:
                ack = json.loads(ack_line.decode())
                self._sub_id = ack.get('sub_id')
            except json.JSONDecodeError:
                pass

        # Stream events
        while self._running:
            line = await reader.readline()
            if not line:
                logger.info('HubDataSource: hub closed connection, will reconnect')
                break
            event = self._deserialize(line.decode())
            if event is not None:
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
    finally:
        self._sub_id = None
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
```

3. Update `watch_token` / `unwatch_token` to also track locally:

```python
def watch_token(self, token_id: str) -> None:
    """Add token to local filter and relay to hub."""
    self._tickers.add(token_id)
    payload: dict[str, Any] = {'cmd': 'watch_token', 'token_id': token_id}
    if self._sub_id is not None:
        payload['sub_id'] = self._sub_id
    self._send_control(payload)

def unwatch_token(self, token_id: str) -> None:
    """Remove token from local filter and relay to hub."""
    self._tickers.discard(token_id)
    payload: dict[str, Any] = {'cmd': 'unwatch_token', 'token_id': token_id}
    if self._sub_id is not None:
        payload['sub_id'] = self._sub_id
    self._send_control(payload)
```

4. Add `from typing import Any` import.

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add coinjure/hub/subscriber.py tests/test_hub_ticker_filter.py
git commit -m "feat(hub): HubDataSource sends subscribe with tickers on connect"
```

---

### Task 5: Wire up — engine passes watch_tokens to HubDataSource on construction

**Files:**

- Modify: `coinjure/cli/engine_commands.py` (where HubDataSource is constructed for paper-run)
- Test: manual — run `poetry run coinjure engine paper-run --all-relations` and verify hub doesn't crash

**Step 1: Find where HubDataSource is created**

Search for `HubDataSource(` in `engine_commands.py` or `runner.py` — the place where `--all-relations` creates the data source for each engine.

**Step 2: Pass strategy's watch_tokens to HubDataSource constructor**

Where HubDataSource is instantiated for paper-run, pass the strategy's `watch_tokens()`:

```python
# Before:
data_source = HubDataSource(hub_socket)
# After:
data_source = HubDataSource(hub_socket, tickers=strategy.watch_tokens())
```

This ensures the subscribe message includes the right tickers from the start.

**Step 3: Run existing tests**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All pass (no regressions)

**Step 4: Commit**

```bash
git add coinjure/cli/engine_commands.py
git commit -m "feat(hub): pass strategy watch_tokens to HubDataSource on construction"
```

---

### Task 6: Integration test — hub + subscriber with filtering

**Files:**

- Modify: `tests/test_hub_ticker_filter.py`

**Step 1: Write integration test**

```python
class TestHubSubscriberIntegration:
    """End-to-end: hub + HubDataSource with ticker filtering."""

    @pytest.mark.asyncio
    async def test_subscriber_only_receives_filtered_events(self, tmp_path: Path) -> None:
        from coinjure.hub.subscriber import HubDataSource

        # Create a mock data source that emits two events
        events = [
            OrderBookEvent(
                ticker=_poly_ticker('tok_A'),
                price=Decimal('0.5'), size=Decimal('100'),
                size_delta=Decimal('100'), side='bid',
            ),
            OrderBookEvent(
                ticker=_poly_ticker('tok_B'),
                price=Decimal('0.6'), size=Decimal('200'),
                size_delta=Decimal('200'), side='ask',
            ),
        ]
        event_iter = iter(events)

        mock_source = MagicMock()
        mock_source.start = asyncio.coroutine(lambda: None)
        mock_source.stop = asyncio.coroutine(lambda: None)

        call_count = 0
        async def get_next():
            nonlocal call_count
            call_count += 1
            if call_count <= len(events):
                return events[call_count - 1]
            await asyncio.sleep(10)  # block after events exhausted
            return None

        mock_source.get_next_event = get_next

        socket_path = tmp_path / 'hub.sock'
        hub = MarketDataHub(socket_path, mock_source)

        # Start hub in background
        hub_task = asyncio.create_task(hub.start())
        await asyncio.sleep(0.1)  # let hub bind

        # Connect subscriber filtering only tok_A
        sub = HubDataSource(socket_path, tickers=['tok_A'])
        await sub.start()
        await asyncio.sleep(0.5)  # let events flow

        # Should only receive tok_A event
        received = []
        while True:
            evt = await sub.get_next_event()
            if evt is None:
                break
            received.append(evt)

        assert len(received) == 1
        assert received[0].ticker.symbol == 'tok_A'

        await sub.stop()
        await hub.stop()
        hub_task.cancel()
        try:
            await hub_task
        except asyncio.CancelledError:
            pass
```

**Step 2: Run test**

Run: `poetry run python -m pytest tests/test_hub_ticker_filter.py::TestHubSubscriberIntegration -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_hub_ticker_filter.py
git commit -m "test(hub): add integration test for subscriber ticker filtering"
```

---

### Task 7: Run full test suite, verify no regressions

**Step 1: Run all tests**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All pass (234+ tests)

**Step 2: Run existing hub tests specifically**

Run: `poetry run python -m pytest tests/test_hub_watch_token.py -v`
Expected: All pass

**Step 3: Final commit if any fixups needed**
