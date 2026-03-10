"""Test that ControlServer can respond to commands while the engine processes events.

Verifies that the engine's main loop yields to the asyncio event loop often enough
for the ControlServer to handle incoming "status" commands without starving.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from coinjure.data.manager import DataManager
from coinjure.data.source import DataSource
from coinjure.engine.control import ControlServer
from coinjure.engine.engine import TradingEngine
from coinjure.engine.trader.paper import PaperTrader
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import IdleStrategy
from coinjure.ticker import CashTicker, PolyMarketTicker
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import NoRiskManager

# ---------------------------------------------------------------------------
# Fake data source that produces events from an async queue
# ---------------------------------------------------------------------------


class QueueDataSource(DataSource):
    """A data source backed by an asyncio.Queue.

    Events are produced as fast as the engine can consume them.
    Returns None (pause) when the queue is empty and ``_finished`` is set,
    otherwise blocks briefly waiting for more events.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._finished = False

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            if self._finished:
                return None
            # Still alive — just nothing right now; let engine loop again.
            return None

    def enqueue(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def finish(self) -> None:
        """Signal that no more events will be produced."""
        self._finished = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICKER = PolyMarketTicker(
    symbol='TEST_YES',
    name='Test Market YES',
    token_id='tok_yes',
    market_id='mkt_1',
    side='yes',
)


def _make_orderbook_event(i: int) -> OrderBookEvent:
    """Create a simple OrderBookEvent with varying price."""
    base = Decimal('0.50') + Decimal(str((i % 10) * 0.001))
    side = 'bid' if i % 2 == 0 else 'ask'
    return OrderBookEvent(
        ticker=_TICKER,
        price=base,
        size=Decimal('100'),
        size_delta=Decimal('5'),
        side=side,
    )


def _make_price_event(i: int) -> PriceChangeEvent:
    price = Decimal('0.50') + Decimal(str((i % 20) * 0.002))
    return PriceChangeEvent(ticker=_TICKER, price=price)


def _build_engine(data_source: DataSource) -> TradingEngine:
    """Build a minimal TradingEngine wired to the given data source."""
    market_data = DataManager()
    risk_manager = NoRiskManager()
    position_manager = PositionManager()

    # Seed cash position so the engine doesn't blow up.
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('1'),
        max_fill_rate=Decimal('1'),
        commission_rate=Decimal('0'),
    )
    strategy = IdleStrategy()

    return TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        initial_capital=Decimal('10000'),
        continuous=True,
    )


async def _send_status(socket_path: Path, timeout: float = 2.0) -> dict:
    """Connect to the control socket and send a 'status' command."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        payload = json.dumps({'cmd': 'status'}) + '\n'
        writer.write(payload.encode())
        await writer.drain()
        writer.write_eof()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(raw.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_server_responds_during_event_processing() -> None:
    """ControlServer must respond to 'status' while the engine processes 1500+ events."""

    # Unique socket path to avoid collisions with other tests / engines.
    sock_path = (
        Path(tempfile.gettempdir()) / f'test_control_{uuid.uuid4().hex[:8]}.sock'
    )

    data_source = QueueDataSource()
    engine = _build_engine(data_source)
    control = ControlServer(engine, socket_path=sock_path)

    # Pre-fill the queue with a large batch of events so the engine is busy.
    num_events = 1500
    for i in range(num_events):
        if i % 3 == 0:
            data_source.enqueue(_make_price_event(i))
        else:
            data_source.enqueue(_make_orderbook_event(i))

    try:
        await control.start()

        async def _run_engine() -> None:
            """Run the engine; it will process all queued events then idle."""
            await engine.start()

        async def _probe_and_stop() -> dict:
            """Wait until the engine has started processing, send status, then stop."""
            # Give the engine a moment to begin consuming events.
            await asyncio.sleep(0.1)

            # Send the status command — this is the core assertion target.
            response = await _send_status(sock_path, timeout=2.0)

            # Tell the data source there are no more events and stop the engine
            # so the test doesn't hang.
            data_source.finish()
            await engine.stop()
            return response

        # Run engine + probe concurrently, with overall timeout safety net.
        _, response = await asyncio.wait_for(
            asyncio.gather(_run_engine(), _probe_and_stop()),
            timeout=15.0,
        )

        # Validate the response.
        assert response['ok'] is True, f'Expected ok=True, got {response}'
        assert 'event_count' in response
        assert 'paused' in response
        assert response['paused'] is False

    finally:
        await control.stop()
        sock_path.unlink(missing_ok=True)
