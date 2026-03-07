from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from coinjure.cli.control import ControlServer
from coinjure.data.data_manager import DataManager
from coinjure.data.order_book import Level, OrderBook
from coinjure.engine.trader.paper_trader import PaperTrader
from coinjure.engine.trader.position_manager import Position, PositionManager
from coinjure.engine.trader.risk_manager import NoRiskManager
from coinjure.engine.trader.types import OrderFailureReason, TradeSide
from coinjure.ticker import CashTicker, PolyMarketTicker


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )


@pytest.fixture
def paper_trader(test_ticker: PolyMarketTicker) -> PaperTrader:
    mdm = DataManager()
    ob = OrderBook()
    ob.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = ob

    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return PaperTrader(
        market_data=mdm,
        risk_manager=NoRiskManager(),
        position_manager=pm,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )


@pytest.mark.asyncio
async def test_duplicate_client_order_id_rejected(
    paper_trader: PaperTrader, test_ticker: PolyMarketTicker
):
    first = await paper_trader.place_order(
        side=TradeSide.BUY,
        ticker=test_ticker,
        limit_price=Decimal('0.55'),
        quantity=Decimal('10'),
        client_order_id='dup-1',
    )
    second = await paper_trader.place_order(
        side=TradeSide.BUY,
        ticker=test_ticker,
        limit_price=Decimal('0.55'),
        quantity=Decimal('10'),
        client_order_id='dup-1',
    )
    assert first.order is not None
    assert second.order is None
    assert second.failure_reason == OrderFailureReason.DUPLICATE_ORDER


@pytest.mark.asyncio
async def test_read_only_blocks_new_orders(
    paper_trader: PaperTrader, test_ticker: PolyMarketTicker
):
    paper_trader.set_read_only(True)
    result = await paper_trader.place_order(
        side=TradeSide.BUY,
        ticker=test_ticker,
        limit_price=Decimal('0.55'),
        quantity=Decimal('10'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.TRADING_DISABLED


@pytest.mark.asyncio
async def test_kill_switch_file_blocks_orders(
    monkeypatch, tmp_path, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
):
    kill_file = tmp_path / 'kill.switch'
    kill_file.write_text('1\n')
    monkeypatch.setenv('PRED_MARKET_CLI_KILL_SWITCH_FILE', str(kill_file))

    result = await paper_trader.place_order(
        side=TradeSide.BUY,
        ticker=test_ticker,
        limit_price=Decimal('0.55'),
        quantity=Decimal('10'),
    )
    assert result.order is None
    assert result.failure_reason == OrderFailureReason.TRADING_DISABLED


def test_control_pause_resume_toggles_read_only(tmp_path):
    class DummyStrategy:
        def __init__(self) -> None:
            self.paused = False

        def set_paused(self, paused: bool) -> None:
            self.paused = paused

    class DummyTrader:
        def __init__(self) -> None:
            self.read_only = False

        def set_read_only(self, enabled: bool) -> None:
            self.read_only = enabled

    strategy = DummyStrategy()
    trader = DummyTrader()
    engine = SimpleNamespace(strategy=strategy, trader=trader)
    server = ControlServer(engine=engine, socket_path=tmp_path / 'engine.sock')

    pause = server._cmd_pause()
    assert pause['status'] == 'paused'
    assert strategy.paused is True
    assert trader.read_only is True

    resume = server._cmd_resume()
    assert resume['status'] == 'running'
    assert strategy.paused is False
    assert trader.read_only is False
