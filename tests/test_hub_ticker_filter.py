"""Tests for hub-side per-subscriber ticker filtering."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coinjure.events import OrderBookEvent, PriceChangeEvent
from coinjure.hub.hub import MarketDataHub
from coinjure.ticker import KalshiTicker, PolyMarketTicker


def _poly_ticker(token_id: str = 'tok_A') -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol=token_id,
        name='Test',
        token_id=token_id,
        market_id='m1',
        event_id='e1',
        side='yes',
    )


def _kalshi_ticker(market_ticker: str = 'KXFOO') -> KalshiTicker:
    return KalshiTicker(
        symbol=market_ticker,
        name='Test',
        market_ticker=market_ticker,
        event_ticker='KXFOO',
        series_ticker='KXFOO',
        side='yes',
    )


class TestFanLoopFiltering:
    """_fan_one should only enqueue events matching a subscriber's ticker filter."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    def test_subscriber_with_matching_filter_receives_event(
        self, hub: MarketDataHub
    ) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = {'tok_A'}

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'),
            size=Decimal('100'),
            size_delta=Decimal('100'),
            side='bid',
        )
        hub._fan_one(event)
        assert not q.empty()

    def test_subscriber_with_non_matching_filter_skips_event(
        self, hub: MarketDataHub
    ) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = {'tok_B'}

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'),
            size=Decimal('100'),
            size_delta=Decimal('100'),
            side='bid',
        )
        hub._fan_one(event)
        assert q.empty()

    def test_subscriber_with_empty_filter_receives_nothing(
        self, hub: MarketDataHub
    ) -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        sub_id = 0
        hub._subscribers[sub_id] = q
        hub._sub_filters[sub_id] = set()

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'),
            size=Decimal('100'),
            size_delta=Decimal('100'),
            side='bid',
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
            price=Decimal('0.5'),
            size=Decimal('100'),
            size_delta=Decimal('100'),
            side='bid',
        )
        hub._fan_one(event)
        assert not q1.empty()
        assert q2.empty()

    def test_no_filter_entry_receives_all(self, hub: MarketDataHub) -> None:
        """Subscriber with no entry in _sub_filters (filter is None) gets all events."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        hub._subscribers[0] = q
        # No entry in _sub_filters for sub_id 0

        event = OrderBookEvent(
            ticker=_poly_ticker('tok_A'),
            price=Decimal('0.5'),
            size=Decimal('100'),
            size_delta=Decimal('100'),
            side='bid',
        )
        hub._fan_one(event)
        assert not q.empty()


class TestSubscribeProtocol:
    """Subscriber sends subscribe command on connect, hub registers filter."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    @pytest.mark.asyncio
    async def test_subscribe_registers_filter_and_acks(
        self, hub: MarketDataHub
    ) -> None:
        """Subscribe command should register filter and return sub_id."""
        raw = (
            json.dumps({'cmd': 'subscribe', 'tickers': ['tok_A', 'tok_B']}).encode()
            + b'\n'
        )
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()

        written_data: list[bytes] = []
        writer = MagicMock()
        writer.write = lambda data: written_data.append(data)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await hub._handle_connection(reader, writer)

        # Check ack response
        assert len(written_data) >= 1
        resp = json.loads(written_data[0].decode().strip())
        assert resp['ok'] is True
        assert 'sub_id' in resp

    @pytest.mark.asyncio
    async def test_subscribe_with_empty_tickers(self, hub: MarketDataHub) -> None:
        raw = json.dumps({'cmd': 'subscribe', 'tickers': []}).encode() + b'\n'
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()

        written_data: list[bytes] = []
        writer = MagicMock()
        writer.write = lambda data: written_data.append(data)
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await hub._handle_connection(reader, writer)

        resp = json.loads(written_data[0].decode().strip())
        assert resp['ok'] is True


class TestWatchTokenFilterUpdate:
    """watch_token/unwatch_token with sub_id should update subscriber filter."""

    @pytest.fixture
    def hub(self, tmp_path: Path) -> MarketDataHub:
        source = MagicMock()
        source.watch_token = MagicMock()
        source.unwatch_token = MagicMock()
        return MarketDataHub(tmp_path / 'hub.sock', source)

    @pytest.mark.asyncio
    async def test_watch_token_with_sub_id_adds_to_filter(
        self, hub: MarketDataHub
    ) -> None:
        hub._sub_filters[0] = {'tok_A'}
        payload = (
            json.dumps(
                {'cmd': 'watch_token', 'token_id': 'tok_B', 'sub_id': 0}
            ).encode()
            + b'\n'
        )
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload, writer)

        assert 'tok_B' in hub._sub_filters[0]
        assert 'tok_A' in hub._sub_filters[0]

    @pytest.mark.asyncio
    async def test_unwatch_token_with_sub_id_removes_from_filter(
        self, hub: MarketDataHub
    ) -> None:
        hub._sub_filters[0] = {'tok_A', 'tok_B'}
        payload = (
            json.dumps(
                {'cmd': 'unwatch_token', 'token_id': 'tok_A', 'sub_id': 0}
            ).encode()
            + b'\n'
        )
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload, writer)

        assert 'tok_A' not in hub._sub_filters[0]
        assert 'tok_B' in hub._sub_filters[0]

    @pytest.mark.asyncio
    async def test_watch_token_without_sub_id_only_updates_source(
        self, hub: MarketDataHub
    ) -> None:
        """Without sub_id, only the source's watch_token is called."""
        hub._sub_filters[0] = {'tok_A'}
        payload = (
            json.dumps({'cmd': 'watch_token', 'token_id': 'tok_B'}).encode() + b'\n'
        )
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        await hub._handle_control(payload, writer)

        hub._source.watch_token.assert_called_once_with('tok_B')
        assert 'tok_B' not in hub._sub_filters[0]


from coinjure.hub.subscriber import HubDataSource


class TestHubDataSourceSubscribe:
    """HubDataSource should track tickers locally and send subscribe on connect."""

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

    def test_sub_id_initially_none(self, tmp_path: Path) -> None:
        src = HubDataSource(tmp_path / 'hub.sock')
        assert src._sub_id is None


class TestHubSubscriberIntegration:
    """End-to-end: hub + HubDataSource with ticker filtering."""

    @pytest.mark.asyncio
    async def test_subscriber_only_receives_filtered_events(self) -> None:
        import shutil
        import tempfile

        # Use a short socket path to avoid AF_UNIX path length limits
        sock_dir = tempfile.mkdtemp(prefix='hub')
        socket_path = Path(sock_dir) / 'h.sock'

        events_to_emit = [
            OrderBookEvent(
                ticker=_poly_ticker('tok_A'),
                price=Decimal('0.5'),
                size=Decimal('100'),
                size_delta=Decimal('100'),
                side='bid',
            ),
            OrderBookEvent(
                ticker=_poly_ticker('tok_B'),
                price=Decimal('0.6'),
                size=Decimal('200'),
                size_delta=Decimal('200'),
                side='ask',
            ),
        ]

        # Gate event emission so events aren't fanned out before subscriber connects
        ready = asyncio.Event()
        call_count = 0

        async def get_next():
            nonlocal call_count
            await ready.wait()
            if call_count < len(events_to_emit):
                evt = events_to_emit[call_count]
                call_count += 1
                return evt
            # Block after events exhausted
            await asyncio.sleep(10)
            return None

        mock_source = MagicMock()
        mock_source.start = AsyncMock()
        mock_source.stop = AsyncMock()
        mock_source.get_next_event = get_next

        hub = MarketDataHub(socket_path, mock_source)

        # Start hub in background
        hub_task = asyncio.create_task(hub.start())
        await asyncio.sleep(0.2)  # let hub bind

        # Connect subscriber filtering only tok_A
        sub = HubDataSource(socket_path, tickers=['tok_A'])
        await sub.start()
        await asyncio.sleep(0.3)  # let subscriber connect and register

        # Now release events so they fan out to the connected subscriber
        ready.set()
        await asyncio.sleep(0.5)  # let events flow through

        # Collect received events
        received = []
        for _ in range(5):
            evt = await sub.get_next_event()
            if evt is None:
                break
            received.append(evt)

        assert len(received) == 1
        assert received[0].ticker.symbol == 'tok_A'

        await sub.stop()
        await hub.stop()
        try:
            await asyncio.wait_for(hub_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            hub_task.cancel()
            try:
                await hub_task
            except asyncio.CancelledError:
                pass
        finally:
            shutil.rmtree(sock_dir, ignore_errors=True)
