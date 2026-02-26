from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pred_market_cli.data.market_data_manager import MarketDataManager
from pred_market_cli.events.events import OrderBookEvent, PriceChangeEvent
from pred_market_cli.order.order_book import Level, OrderBook
from pred_market_cli.position.position_manager import Position, PositionManager
from pred_market_cli.risk.risk_manager import NoRiskManager
from pred_market_cli.strategy.market_making_strategy import MarketMakingStrategy
from pred_market_cli.strategy.mean_reversion_strategy import MeanReversionStrategy
from pred_market_cli.strategy.momentum_strategy import MomentumStrategy
from pred_market_cli.strategy.orderbook_imbalance_strategy import (
    OrderBookImbalanceStrategy,
)
from pred_market_cli.strategy.simple_strategy import LLMDecision, SimpleStrategy
from pred_market_cli.strategy.strategy import Strategy, StrategyDecision
from pred_market_cli.ticker.ticker import CashTicker, PolyMarketTicker
from pred_market_cli.trader.paper_trader import PaperTrader


class DummyStrategy(Strategy):
    async def process_event(self, event, trader) -> None:  # type: ignore[no-untyped-def]
        return


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
def other_ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='OTHER_TOKEN',
        name='Other Market',
        token_id='token999',
        market_id='market999',
        event_id='event999',
    )


@pytest.fixture
def market_data(test_ticker: PolyMarketTicker) -> MarketDataManager:
    mdm = MarketDataManager()
    ob = OrderBook()
    ob.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = ob
    return mdm


@pytest.fixture
def position_manager() -> PositionManager:
    pm = PositionManager()
    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    return pm


@pytest.fixture
def paper_trader(
    market_data: MarketDataManager, position_manager: PositionManager
) -> PaperTrader:
    return PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )


def _set_order_book(
    md: MarketDataManager,
    ticker: PolyMarketTicker,
    *,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> None:
    ob = OrderBook()
    ob.update(
        bids=[Level(price=Decimal(p), size=Decimal(s)) for p, s in bids],
        asks=[Level(price=Decimal(p), size=Decimal(s)) for p, s in asks],
    )
    md.order_books[ticker] = ob


def _open_yes_position(
    pm: PositionManager, ticker: PolyMarketTicker, qty: str = '10'
) -> None:
    pm.update_position(
        Position(
            ticker=ticker,
            quantity=Decimal(qty),
            average_cost=Decimal('0.50'),
            realized_pnl=Decimal('0'),
        )
    )


def test_strategy_interface_defaults() -> None:
    decision = StrategyDecision(
        timestamp='12:00:00',
        ticker_name='Test',
        action='HOLD',
        executed=False,
    )
    assert decision.signal_values == {}
    assert decision.confidence == 0.0

    base = DummyStrategy()
    assert base.get_decisions() == []
    assert base.get_decision_stats() == {}


def test_simple_strategy_decision_wrapping() -> None:
    strategy = SimpleStrategy()
    strategy.decisions.append(
        LLMDecision(
            timestamp='12:34:56',
            ticker_name='My Market',
            action='BUY_YES',
            confidence=0.8,
            executed=True,
            reasoning='edge positive',
            llm_prob=0.72,
            market_price=0.55,
        )
    )
    strategy.total_decisions = 1
    strategy.total_executed = 1
    strategy.total_buy_yes = 1
    strategy.total_buy_no = 0
    strategy.total_holds = 0
    strategy.total_closes = 0

    wrapped = strategy.get_decisions()
    assert len(wrapped) == 1
    assert wrapped[0].signal_values['llm_prob'] == 0.72
    assert wrapped[0].signal_values['market_price'] == 0.55
    assert wrapped[0].signal_values['edge'] == 0.17

    stats = strategy.get_decision_stats()
    assert set(stats.keys()) == {
        'decisions',
        'executed',
        'buy_yes',
        'buy_no',
        'holds',
        'closes',
    }


@pytest.mark.asyncio
async def test_obi_balanced_no_trade(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = OrderBookImbalanceStrategy(tickers=[test_ticker], entry_threshold=0.3)
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '100'), ('0.49', '100')],
        asks=[('0.51', '100'), ('0.52', '100')],
    )
    event = OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0'))
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decision_stats()['decisions'] == 1
    assert strategy.get_decisions()[-1].action == 'HOLD'


@pytest.mark.asyncio
async def test_obi_entry_exit_and_guards(
    test_ticker: PolyMarketTicker,
    other_ticker: PolyMarketTicker,
    paper_trader: PaperTrader,
) -> None:
    strategy = OrderBookImbalanceStrategy(
        tickers=[test_ticker],
        depth=3,
        entry_threshold=0.3,
        exit_threshold=-0.1,
        max_hold_seconds=1,
    )

    _set_order_book(
        paper_trader.market_data,
        other_ticker,
        bids=[('0.50', '500')],
        asks=[('0.51', '500')],
    )
    await strategy.process_event(
        OrderBookEvent(other_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decision_stats()['decisions'] == 0

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '1200'), ('0.49', '200')],
        asks=[('0.51', '200'), ('0.52', '100')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'BUY_YES'
    assert strategy.get_decisions()[-1].executed is True

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.49', '100')],
        asks=[('0.50', '1000'), ('0.51', '700')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.49'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'CLOSE_OBI'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = datetime.now() - timedelta(seconds=10)
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '500')],
        asks=[('0.51', '500')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = datetime.now()
    strategy._closing_in_progress.add(test_ticker.symbol)
    prev_orders = len(paper_trader.orders)
    prev_decisions = strategy.get_decision_stats()['decisions']
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert len(paper_trader.orders) == prev_orders
    assert strategy.get_decision_stats()['decisions'] == prev_decisions


@pytest.mark.asyncio
async def test_momentum_entry_exit_and_timeout(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MomentumStrategy(
        tickers=[test_ticker],
        window=3,
        entry_threshold=0.02,
        exit_threshold=-0.01,
        max_hold_seconds=1,
    )
    event = lambda p: PriceChangeEvent(test_ticker, Decimal(p))

    await strategy.process_event(event('0.50'), paper_trader)
    await strategy.process_event(event('0.51'), paper_trader)
    assert strategy.get_decision_stats()['decisions'] == 0

    await strategy.process_event(event('0.53'), paper_trader)
    assert strategy.get_decisions()[-1].action == 'BUY_YES'

    await strategy.process_event(event('0.52'), paper_trader)
    await strategy.process_event(event('0.50'), paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_MOM'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = datetime.now() - timedelta(seconds=10)
    await strategy.process_event(event('0.50'), paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'


@pytest.mark.asyncio
async def test_mean_reversion_signals_and_guards(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MeanReversionStrategy(
        tickers=[test_ticker],
        window=4,
        entry_z_score=1.0,
        exit_z_score=0.3,
        max_hold_seconds=1,
    )
    event = lambda p: PriceChangeEvent(test_ticker, Decimal(p))

    await strategy.process_event(event('0.50'), paper_trader)
    await strategy.process_event(event('0.50'), paper_trader)
    await strategy.process_event(event('0.50'), paper_trader)
    await strategy.process_event(event('0.50'), paper_trader)
    assert strategy.get_decision_stats()['decisions'] == 0

    for p in ['0.60', '0.60', '0.60', '0.40']:
        await strategy.process_event(event(p), paper_trader)
    assert strategy.get_decisions()[-1].action == 'BUY_YES'

    for p in ['0.40', '0.40', '0.40', '0.65']:
        await strategy.process_event(event(p), paper_trader)
    assert strategy.get_decisions()[-1].action == 'SELL_YES'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = (datetime.now(), 'long')
    for p in ['0.49', '0.50', '0.51', '0.50']:
        await strategy.process_event(event(p), paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_MR'

    _open_yes_position(paper_trader.position_manager, test_ticker, '5')
    strategy._entries[test_ticker.symbol] = (
        datetime.now() - timedelta(seconds=10),
        'long',
    )
    for p in ['0.49', '0.50', '0.51', '0.50']:
        await strategy.process_event(event(p), paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'


def test_mean_reversion_window_guard() -> None:
    with pytest.raises(ValueError):
        MeanReversionStrategy(tickers=[], window=1)


@pytest.mark.asyncio
async def test_market_making_entry_and_exits(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MarketMakingStrategy(
        tickers=[test_ticker],
        min_spread=Decimal('0.05'),
        take_profit_pct=0.5,
        stop_loss_pct=0.02,
        max_hold_seconds=1,
        tick=Decimal('0.01'),
    )
    event = OrderBookEvent(test_ticker, Decimal('0.50'), Decimal('100'), Decimal('0'))

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.50', '1000')],
        asks=[('0.53', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'HOLD'

    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.45', '1000')],
        asks=[('0.55', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'BUY_YES'
    assert test_ticker.symbol in strategy._entries

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now(),
        Decimal('0.46'),
        Decimal('0.50'),
        Decimal('0.40'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.51', '1000')],
        asks=[('0.56', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TP'

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now(),
        Decimal('0.46'),
        Decimal('0.50'),
        Decimal('0.44'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.43', '1000')],
        asks=[('0.48', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_SL'

    _open_yes_position(paper_trader.position_manager, test_ticker, '10')
    strategy._entries[test_ticker.symbol] = (
        datetime.now() - timedelta(seconds=10),
        Decimal('0.46'),
        Decimal('0.60'),
        Decimal('0.40'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.47', '1000')],
        asks=[('0.56', '1000')],
    )
    await strategy.process_event(event, paper_trader)
    assert strategy.get_decisions()[-1].action == 'CLOSE_TIMEOUT'


@pytest.mark.asyncio
async def test_market_making_no_cross_spread_entry(
    test_ticker: PolyMarketTicker, paper_trader: PaperTrader
) -> None:
    strategy = MarketMakingStrategy(
        tickers=[test_ticker],
        min_spread=Decimal('0.005'),
        tick=Decimal('0.01'),
    )
    _set_order_book(
        paper_trader.market_data,
        test_ticker,
        bids=[('0.49', '1000')],
        asks=[('0.50', '1000')],
    )
    await strategy.process_event(
        OrderBookEvent(test_ticker, Decimal('0.49'), Decimal('100'), Decimal('0')),
        paper_trader,
    )
    assert strategy.get_decisions()[-1].action == 'HOLD'
    assert strategy.get_decisions()[-1].executed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('factory', 'event_factory'),
    [
        (
            lambda t: OrderBookImbalanceStrategy(tickers=[t]),
            lambda t: OrderBookEvent(t, Decimal('0.50'), Decimal('100'), Decimal('0')),
        ),
        (
            lambda t: MomentumStrategy(tickers=[t], window=2),
            lambda t: PriceChangeEvent(t, Decimal('0.50')),
        ),
        (
            lambda t: MeanReversionStrategy(tickers=[t], window=3),
            lambda t: PriceChangeEvent(t, Decimal('0.50')),
        ),
        (
            lambda t: MarketMakingStrategy(tickers=[t]),
            lambda t: OrderBookEvent(t, Decimal('0.50'), Decimal('100'), Decimal('0')),
        ),
    ],
)
async def test_quant_strategies_respect_pause(
    test_ticker: PolyMarketTicker,
    paper_trader: PaperTrader,
    factory,
    event_factory,
) -> None:
    strategy = factory(test_ticker)
    strategy.set_paused(True)
    before_orders = len(paper_trader.orders)
    await strategy.process_event(event_factory(test_ticker), paper_trader)
    assert strategy.get_decisions() == []
    assert strategy.get_decision_stats().get('decisions', 0) == 0
    assert len(paper_trader.orders) == before_orders
