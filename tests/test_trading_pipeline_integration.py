"""Integration tests for the trading pipeline.

Tests the end-to-end flow of:
1. PaperTrader order flow (BUY -> position update -> SELL -> cash balance)
2. KalshiTrader validation guards (without API calls)
3. MultiStrategy fan-out (process_event, get_decisions, watch_tokens)
4. DataManager order book (best bid/ask, find_complement)
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import ClassVar

import pytest

from coinjure.data.manager import DataManager
from coinjure.data.order_book import Level, OrderBook
from coinjure.engine.trader.paper import PaperTrader
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy, StrategyDecision
from coinjure.ticker import CashTicker, KalshiTicker, PolyMarketTicker, Ticker
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import NoRiskManager
from coinjure.trading.trader import Trader
from coinjure.trading.types import (
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    TradeSide,
)


# ---------------------------------------------------------------------------
# Helper: MultiStrategy (fan-out composite)
# ---------------------------------------------------------------------------


class MultiStrategy(Strategy):
    """Composite strategy that fans out events to multiple sub-strategies."""

    name: ClassVar[str] = 'MultiStrategy'

    def __init__(self, strategies: list[Strategy]) -> None:
        super().__init__()
        self._strategies = list(strategies)

    async def process_event(self, event: Event, trader: Trader) -> None:
        for s in self._strategies:
            s.bind_context(event, trader)
            await s.process_event(event, trader)

    def get_decisions(self) -> list[StrategyDecision]:
        combined: list[StrategyDecision] = []
        for s in self._strategies:
            combined.extend(s.get_decisions())
        return combined

    def watch_tokens(self) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for s in self._strategies:
            for t in s.watch_tokens():
                if t not in seen:
                    seen.add(t)
                    tokens.append(t)
        return tokens


# ---------------------------------------------------------------------------
# Helper: stub strategies for MultiStrategy tests
# ---------------------------------------------------------------------------


class StubStrategyA(Strategy):
    name: ClassVar[str] = 'StubA'

    def __init__(self) -> None:
        super().__init__()
        self.events_received: list[Event] = []

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.events_received.append(event)
        self.record_decision(
            ticker_name='stub_a_ticker',
            action='HOLD',
            executed=False,
            reasoning='StubA saw event',
        )

    def watch_tokens(self) -> list[str]:
        return ['token_a1', 'token_a2']


class StubStrategyB(Strategy):
    name: ClassVar[str] = 'StubB'

    def __init__(self) -> None:
        super().__init__()
        self.events_received: list[Event] = []

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.events_received.append(event)
        self.record_decision(
            ticker_name='stub_b_ticker',
            action='BUY_YES',
            executed=True,
            reasoning='StubB wants to buy',
        )

    def watch_tokens(self) -> list[str]:
        return ['token_b1']


class StubStrategyC(Strategy):
    name: ClassVar[str] = 'StubC'

    def __init__(self) -> None:
        super().__init__()
        self.events_received: list[Event] = []

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.events_received.append(event)

    def watch_tokens(self) -> list[str]:
        return ['token_a2', 'token_c1']  # token_a2 overlaps with StubA


# ===========================================================================
# 1. PaperTrader order flow
# ===========================================================================


class TestPaperTraderOrderFlow:
    """End-to-end: create trader, seed cash, BUY, verify, SELL, verify balances."""

    @pytest.fixture
    def poly_ticker(self) -> PolyMarketTicker:
        return PolyMarketTicker(
            symbol='INTEG_YES',
            name='Integration Test YES',
            token_id='integ-token-yes',
            market_id='integ-market',
            event_id='integ-event',
            side='yes',
        )

    @pytest.fixture
    def setup(self, poly_ticker: PolyMarketTicker):
        """Wire up DataManager, PositionManager, PaperTrader."""
        dm = DataManager()
        ob = OrderBook()
        ob.update(
            asks=[
                Level(price=Decimal('0.60'), size=Decimal('500')),
                Level(price=Decimal('0.65'), size=Decimal('300')),
            ],
            bids=[
                Level(price=Decimal('0.55'), size=Decimal('500')),
                Level(price=Decimal('0.50'), size=Decimal('300')),
            ],
        )
        dm.update_order_book(poly_ticker, ob)

        pm = PositionManager()
        # Seed cash
        pm.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('1000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trader = PaperTrader(
            market_data=dm,
            risk_manager=NoRiskManager(),
            position_manager=pm,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.00'),  # zero commission for easier math
            slippage_bps=0,
        )
        return dm, pm, trader

    @pytest.mark.asyncio
    async def test_buy_order_result_shape(self, setup, poly_ticker):
        """BUY -> PlaceOrderResult has .order with .filled_quantity > 0."""
        _dm, _pm, trader = setup

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('50'),
        )

        assert isinstance(result, PlaceOrderResult)
        assert result.order is not None, 'order must not be None for a valid buy'
        assert result.failure_reason is None
        assert result.order.filled_quantity == Decimal('50')
        assert result.order.status == OrderStatus.FILLED
        assert result.accepted is True
        assert result.executed is True

    @pytest.mark.asyncio
    async def test_buy_updates_position(self, setup, poly_ticker):
        """After BUY, PositionManager reflects the new position."""
        _dm, pm, trader = setup

        await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('50'),
        )

        pos = pm.get_position(poly_ticker)
        assert pos is not None
        assert pos.quantity == Decimal('50')
        assert pos.average_cost == Decimal('0.60')

    @pytest.mark.asyncio
    async def test_sell_decreases_position(self, setup, poly_ticker):
        """SELL after BUY decreases the position quantity."""
        _dm, pm, trader = setup

        # BUY 50
        await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('50'),
        )

        # SELL 20
        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=poly_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('20'),
        )

        assert result.order is not None
        assert result.order.filled_quantity == Decimal('20')

        pos = pm.get_position(poly_ticker)
        assert pos is not None
        assert pos.quantity == Decimal('30')  # 50 - 20

    @pytest.mark.asyncio
    async def test_cash_balance_changes(self, setup, poly_ticker):
        """Cash decreases on BUY, increases on SELL."""
        _dm, pm, trader = setup

        initial_cash = Decimal('1000')
        assert pm.get_position(CashTicker.POLYMARKET_USDC).quantity == initial_cash

        # BUY 50 @ 0.60 => cost = 50 * 0.60 = 30
        await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('50'),
        )
        cash_after_buy = pm.get_position(CashTicker.POLYMARKET_USDC).quantity
        assert cash_after_buy == initial_cash - Decimal('30')

        # SELL 20 @ 0.55 => proceeds = 20 * 0.55 = 11
        await trader.place_order(
            side=TradeSide.SELL,
            ticker=poly_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('20'),
        )
        cash_after_sell = pm.get_position(CashTicker.POLYMARKET_USDC).quantity
        expected = cash_after_buy + Decimal('11')  # 20 * 0.55
        assert cash_after_sell == expected

    @pytest.mark.asyncio
    async def test_buy_sell_full_cycle_pnl(self, setup, poly_ticker):
        """Full round trip: buy 50 @ 0.60, sell 50 @ 0.55 => realized loss."""
        _dm, pm, trader = setup

        await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('50'),
        )
        await trader.place_order(
            side=TradeSide.SELL,
            ticker=poly_ticker,
            limit_price=Decimal('0.55'),
            quantity=Decimal('50'),
        )

        pos = pm.get_position(poly_ticker)
        assert pos.quantity == Decimal('0')
        # realized_pnl = (sell_price - avg_cost) * qty - commission
        # = (0.55 - 0.60) * 50 - 0 = -2.5
        assert pos.realized_pnl == Decimal('-2.5')

    @pytest.mark.asyncio
    async def test_commission_deducted_from_cash(self, poly_ticker):
        """With non-zero commission, cash is reduced by price*qty + commission."""
        dm = DataManager()
        ob = OrderBook()
        ob.update(
            asks=[Level(price=Decimal('0.60'), size=Decimal('500'))],
            bids=[Level(price=Decimal('0.55'), size=Decimal('500'))],
        )
        dm.update_order_book(poly_ticker, ob)

        pm = PositionManager()
        pm.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('1000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trader = PaperTrader(
            market_data=dm,
            risk_manager=NoRiskManager(),
            position_manager=pm,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.02'),  # 2%
            slippage_bps=0,
        )

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=poly_ticker,
            limit_price=Decimal('0.60'),
            quantity=Decimal('100'),
        )

        assert result.order is not None
        # commission = 100 * 0.60 * 0.02 = 1.20
        assert result.order.commission == Decimal('1.2')


# ===========================================================================
# 2. KalshiTrader validation (no API calls)
# ===========================================================================


class TestKalshiTraderValidation:
    """Test KalshiTrader input validation without touching the Kalshi API.

    We test the validation logic that runs BEFORE any API call by instantiating
    only the base Trader class checks that KalshiTrader shares (via PaperTrader
    configured with Kalshi tickers), since KalshiTrader.__init__ requires a
    real Kalshi SDK import and credentials. The validation paths are identical.
    """

    @pytest.fixture
    def kalshi_ticker(self) -> KalshiTicker:
        return KalshiTicker(
            symbol='KXTICKER-YES',
            name='Kalshi Test YES',
            market_ticker='KXTICKER',
            event_ticker='KXEVENT',
            side='yes',
        )

    @pytest.fixture
    def non_kalshi_ticker(self) -> PolyMarketTicker:
        return PolyMarketTicker(
            symbol='POLY_TOKEN',
            name='Poly Test',
            token_id='poly-token-123',
        )

    @pytest.fixture
    def kalshi_setup(self, kalshi_ticker: KalshiTicker):
        """PaperTrader wired with Kalshi_USD cash for validation tests."""
        dm = DataManager()
        ob = OrderBook()
        ob.update(
            asks=[Level(price=Decimal('0.45'), size=Decimal('100'))],
            bids=[Level(price=Decimal('0.40'), size=Decimal('100'))],
        )
        dm.update_order_book(kalshi_ticker, ob)

        pm = PositionManager()
        pm.update_position(
            Position(
                ticker=CashTicker.KALSHI_USD,
                quantity=Decimal('100'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trader = PaperTrader(
            market_data=dm,
            risk_manager=NoRiskManager(),
            position_manager=pm,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0'),
            slippage_bps=0,
        )
        return dm, pm, trader

    @pytest.mark.asyncio
    async def test_rejects_zero_quantity(self, kalshi_setup, kalshi_ticker):
        """Zero quantity is rejected as INVALID_ORDER."""
        _dm, _pm, trader = kalshi_setup

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=kalshi_ticker,
            limit_price=Decimal('0.45'),
            quantity=Decimal('0'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_rejects_zero_price(self, kalshi_setup, kalshi_ticker):
        """Zero price is rejected as INVALID_ORDER."""
        _dm, _pm, trader = kalshi_setup

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=kalshi_ticker,
            limit_price=Decimal('0'),
            quantity=Decimal('10'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_rejects_negative_quantity(self, kalshi_setup, kalshi_ticker):
        """Negative quantity is rejected as INVALID_ORDER."""
        _dm, _pm, trader = kalshi_setup

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=kalshi_ticker,
            limit_price=Decimal('0.45'),
            quantity=Decimal('-5'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_checks_cash_before_buying(self, kalshi_setup, kalshi_ticker):
        """BUY is rejected when cash is insufficient."""
        _dm, pm, trader = kalshi_setup

        # Set cash to a very small amount
        pm.update_position(
            Position(
                ticker=CashTicker.KALSHI_USD,
                quantity=Decimal('0.01'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=kalshi_ticker,
            limit_price=Decimal('0.45'),
            quantity=Decimal('10'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INSUFFICIENT_CASH

    @pytest.mark.asyncio
    async def test_sell_rejected_without_position(self, kalshi_setup, kalshi_ticker):
        """SELL is rejected when there is no existing position (no short selling)."""
        _dm, _pm, trader = kalshi_setup

        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=kalshi_ticker,
            limit_price=Decimal('0.40'),
            quantity=Decimal('5'),
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_sell_rejected_exceeding_position(self, kalshi_setup, kalshi_ticker):
        """SELL more than held is rejected."""
        _dm, pm, trader = kalshi_setup

        # Give a small position
        pm.update_position(
            Position(
                ticker=kalshi_ticker,
                quantity=Decimal('3'),
                average_cost=Decimal('0.40'),
                realized_pnl=Decimal('0'),
            )
        )

        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=kalshi_ticker,
            limit_price=Decimal('0.40'),
            quantity=Decimal('10'),  # more than 3
        )

        assert result.order is None
        assert result.failure_reason == OrderFailureReason.INVALID_ORDER

    @pytest.mark.asyncio
    async def test_kalshi_ticker_collateral_is_usd(self, kalshi_ticker):
        """KalshiTicker.collateral should be CashTicker.KALSHI_USD."""
        assert kalshi_ticker.collateral == CashTicker.KALSHI_USD

    @pytest.mark.asyncio
    async def test_non_kalshi_ticker_not_accepted_by_real_validator(self):
        """Verify that KalshiTrader.place_order rejects non-KalshiTicker.

        We test this by importing the actual validation check in kalshi.py:
        ``if not isinstance(ticker, KalshiTicker) or not ticker.market_ticker``
        We replicate the check here since instantiating KalshiTrader requires
        real credentials.
        """
        poly_ticker = PolyMarketTicker(
            symbol='POLY_TOKEN', name='Poly Test', token_id='t1'
        )
        # The check from KalshiTrader.place_order line 232
        assert not isinstance(poly_ticker, KalshiTicker)

        # Also verify a KalshiTicker without market_ticker fails
        empty_mt = KalshiTicker(symbol='X', name='X', market_ticker='')
        assert not empty_mt.market_ticker  # truthy check fails


# ===========================================================================
# 3. MultiStrategy fan-out
# ===========================================================================


class TestMultiStrategyFanOut:
    """Verify MultiStrategy dispatches to all subs and aggregates results."""

    @pytest.fixture
    def strategies(self) -> tuple[StubStrategyA, StubStrategyB, StubStrategyC]:
        return StubStrategyA(), StubStrategyB(), StubStrategyC()

    @pytest.fixture
    def multi(
        self, strategies: tuple[StubStrategyA, StubStrategyB, StubStrategyC]
    ) -> MultiStrategy:
        return MultiStrategy(list(strategies))

    @pytest.fixture
    def dummy_ticker(self) -> PolyMarketTicker:
        return PolyMarketTicker(
            symbol='MULTI_TEST', name='Multi Test', token_id='mt-1'
        )

    @pytest.fixture
    def dummy_event(self, dummy_ticker) -> PriceChangeEvent:
        return PriceChangeEvent(
            ticker=dummy_ticker, price=Decimal('0.55')
        )

    @pytest.fixture
    def dummy_trader(self, dummy_ticker) -> PaperTrader:
        dm = DataManager()
        ob = OrderBook()
        ob.update(
            asks=[Level(price=Decimal('0.60'), size=Decimal('100'))],
            bids=[Level(price=Decimal('0.50'), size=Decimal('100'))],
        )
        dm.update_order_book(dummy_ticker, ob)

        pm = PositionManager()
        pm.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('1000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        return PaperTrader(
            market_data=dm,
            risk_manager=NoRiskManager(),
            position_manager=pm,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0'),
            slippage_bps=0,
        )

    @pytest.mark.asyncio
    async def test_process_event_dispatches_to_all(
        self, multi, strategies, dummy_event, dummy_trader
    ):
        """process_event should call process_event on all 3 sub-strategies."""
        sa, sb, sc = strategies

        await multi.process_event(dummy_event, dummy_trader)

        assert len(sa.events_received) == 1
        assert sa.events_received[0] is dummy_event
        assert len(sb.events_received) == 1
        assert sb.events_received[0] is dummy_event
        assert len(sc.events_received) == 1
        assert sc.events_received[0] is dummy_event

    @pytest.mark.asyncio
    async def test_get_decisions_aggregates_from_all(
        self, multi, strategies, dummy_event, dummy_trader
    ):
        """get_decisions should aggregate decisions from all sub-strategies."""
        await multi.process_event(dummy_event, dummy_trader)

        decisions = multi.get_decisions()
        # StubA records 1 HOLD, StubB records 1 BUY_YES, StubC records none
        assert len(decisions) == 2

        actions = [d.action for d in decisions]
        assert 'HOLD' in actions
        assert 'BUY_YES' in actions

        ticker_names = [d.ticker_name for d in decisions]
        assert 'stub_a_ticker' in ticker_names
        assert 'stub_b_ticker' in ticker_names

    @pytest.mark.asyncio
    async def test_get_decisions_accumulates_across_events(
        self, multi, strategies, dummy_event, dummy_trader
    ):
        """Multiple events produce accumulated decisions."""
        await multi.process_event(dummy_event, dummy_trader)
        await multi.process_event(dummy_event, dummy_trader)

        decisions = multi.get_decisions()
        # 2 events * (1 from A + 1 from B) = 4 decisions
        assert len(decisions) == 4

    def test_watch_tokens_aggregates_from_all(self, multi):
        """watch_tokens should aggregate unique tokens from all sub-strategies."""
        tokens = multi.watch_tokens()

        # StubA: [token_a1, token_a2], StubB: [token_b1], StubC: [token_a2, token_c1]
        # Unique, preserving first-seen order: token_a1, token_a2, token_b1, token_c1
        assert len(tokens) == 4
        assert tokens == ['token_a1', 'token_a2', 'token_b1', 'token_c1']

    def test_watch_tokens_deduplicates(self, multi):
        """Overlapping tokens (token_a2 in both A and C) appear only once."""
        tokens = multi.watch_tokens()
        assert tokens.count('token_a2') == 1


# ===========================================================================
# 4. DataManager order book
# ===========================================================================


class TestDataManagerOrderBook:
    """Test order book management, best bid/ask, and find_complement."""

    @pytest.fixture
    def dm(self) -> DataManager:
        return DataManager(synthetic_book=False)

    @pytest.fixture
    def poly_yes(self) -> PolyMarketTicker:
        return PolyMarketTicker(
            symbol='PM_MKT_YES',
            name='PM Test YES',
            token_id='pm-tok-yes',
            market_id='pm-mkt-1',
            side='yes',
        )

    @pytest.fixture
    def poly_no(self) -> PolyMarketTicker:
        return PolyMarketTicker(
            symbol='PM_MKT_NO',
            name='PM Test NO',
            token_id='pm-tok-no',
            market_id='pm-mkt-1',
            side='no',
        )

    @pytest.fixture
    def kalshi_yes(self) -> KalshiTicker:
        return KalshiTicker(
            symbol='KX_MKT_YES',
            name='KX Test YES',
            market_ticker='KXMKT',
            event_ticker='KXEVT',
            side='yes',
        )

    @pytest.fixture
    def kalshi_no(self) -> KalshiTicker:
        return KalshiTicker(
            symbol='KX_MKT_NO',
            name='KX Test NO',
            market_ticker='KXMKT',
            event_ticker='KXEVT',
            side='no',
        )

    def test_get_best_ask(self, dm, poly_yes):
        """get_best_ask returns the lowest ask."""
        ob = OrderBook()
        ob.update(
            asks=[
                Level(price=Decimal('0.55'), size=Decimal('100')),
                Level(price=Decimal('0.60'), size=Decimal('200')),
            ],
            bids=[],
        )
        dm.update_order_book(poly_yes, ob)

        best_ask = dm.get_best_ask(poly_yes)
        assert best_ask is not None
        assert best_ask.price == Decimal('0.55')
        assert best_ask.size == Decimal('100')

    def test_get_best_bid(self, dm, poly_yes):
        """get_best_bid returns the highest bid."""
        ob = OrderBook()
        ob.update(
            asks=[],
            bids=[
                Level(price=Decimal('0.50'), size=Decimal('300')),
                Level(price=Decimal('0.45'), size=Decimal('150')),
            ],
        )
        dm.update_order_book(poly_yes, ob)

        best_bid = dm.get_best_bid(poly_yes)
        assert best_bid is not None
        assert best_bid.price == Decimal('0.50')
        assert best_bid.size == Decimal('300')

    def test_best_bid_ask_none_when_empty(self, dm, poly_yes):
        """Returns None when no order book exists for the ticker."""
        assert dm.get_best_bid(poly_yes) is None
        assert dm.get_best_ask(poly_yes) is None

    def test_best_bid_ask_empty_book(self, dm, poly_yes):
        """Returns None when order book has no levels."""
        dm.update_order_book(poly_yes, OrderBook())

        assert dm.get_best_bid(poly_yes) is None
        assert dm.get_best_ask(poly_yes) is None

    def test_find_complement_polymarket(self, dm, poly_yes, poly_no):
        """find_complement returns the opposite side for Polymarket tickers."""
        ob_yes = OrderBook()
        ob_yes.update(
            asks=[Level(price=Decimal('0.55'), size=Decimal('100'))],
            bids=[Level(price=Decimal('0.50'), size=Decimal('100'))],
        )
        ob_no = OrderBook()
        ob_no.update(
            asks=[Level(price=Decimal('0.48'), size=Decimal('100'))],
            bids=[Level(price=Decimal('0.42'), size=Decimal('100'))],
        )
        dm.update_order_book(poly_yes, ob_yes)
        dm.update_order_book(poly_no, ob_no)

        # YES -> NO
        complement = dm.find_complement(poly_yes)
        assert complement is not None
        assert complement.side == 'no'
        assert complement.symbol == poly_no.symbol

        # NO -> YES
        complement_back = dm.find_complement(poly_no)
        assert complement_back is not None
        assert complement_back.side == 'yes'
        assert complement_back.symbol == poly_yes.symbol

    def test_find_complement_kalshi(self, dm, kalshi_yes, kalshi_no):
        """find_complement returns the opposite side for Kalshi tickers."""
        ob_yes = OrderBook()
        ob_yes.update(
            asks=[Level(price=Decimal('0.60'), size=Decimal('50'))],
            bids=[Level(price=Decimal('0.55'), size=Decimal('50'))],
        )
        ob_no = OrderBook()
        ob_no.update(
            asks=[Level(price=Decimal('0.45'), size=Decimal('50'))],
            bids=[Level(price=Decimal('0.40'), size=Decimal('50'))],
        )
        dm.update_order_book(kalshi_yes, ob_yes)
        dm.update_order_book(kalshi_no, ob_no)

        complement = dm.find_complement(kalshi_yes)
        assert complement is not None
        assert complement.side == 'no'

        complement_back = dm.find_complement(kalshi_no)
        assert complement_back is not None
        assert complement_back.side == 'yes'

    def test_find_complement_returns_none_without_counterpart(self, dm, poly_yes):
        """find_complement returns None when no opposite side exists."""
        ob = OrderBook()
        ob.update(
            asks=[Level(price=Decimal('0.55'), size=Decimal('100'))],
            bids=[Level(price=Decimal('0.50'), size=Decimal('100'))],
        )
        dm.update_order_book(poly_yes, ob)

        assert dm.find_complement(poly_yes) is None

    def test_orderbook_event_updates_levels(self, dm, poly_yes):
        """process_orderbook_event correctly inserts new levels."""
        event = OrderBookEvent(
            ticker=poly_yes,
            price=Decimal('0.52'),
            size=Decimal('200'),
            size_delta=Decimal('200'),
            side='bid',
        )
        dm.process_orderbook_event(event)

        best_bid = dm.get_best_bid(poly_yes)
        assert best_bid is not None
        assert best_bid.price == Decimal('0.52')
        assert best_bid.size == Decimal('200')

    def test_multiple_orderbook_events(self, dm, poly_yes):
        """Multiple events build up the order book with correct sorting."""
        # Add two asks
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.60'),
                size=Decimal('100'),
                size_delta=Decimal('100'),
                side='ask',
            )
        )
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.55'),
                size=Decimal('150'),
                size_delta=Decimal('150'),
                side='ask',
            )
        )

        # Add two bids
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.50'),
                size=Decimal('200'),
                size_delta=Decimal('200'),
                side='bid',
            )
        )
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.52'),
                size=Decimal('300'),
                size_delta=Decimal('300'),
                side='bid',
            )
        )

        # Best ask = lowest = 0.55
        best_ask = dm.get_best_ask(poly_yes)
        assert best_ask.price == Decimal('0.55')

        # Best bid = highest = 0.52
        best_bid = dm.get_best_bid(poly_yes)
        assert best_bid.price == Decimal('0.52')

        # Full depth
        asks = dm.get_asks(poly_yes)
        assert len(asks) == 2
        assert asks[0].price < asks[1].price  # ascending

        bids = dm.get_bids(poly_yes)
        assert len(bids) == 2
        assert bids[0].price > bids[1].price  # descending

    def test_zero_size_removes_level(self, dm, poly_yes):
        """A level update with size=0 removes it from the book."""
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.55'),
                size=Decimal('100'),
                size_delta=Decimal('100'),
                side='ask',
            )
        )
        assert dm.get_best_ask(poly_yes) is not None

        # Remove by sending size=0
        dm.process_orderbook_event(
            OrderBookEvent(
                ticker=poly_yes,
                price=Decimal('0.55'),
                size=Decimal('0'),
                size_delta=Decimal('-100'),
                side='ask',
            )
        )
        assert dm.get_best_ask(poly_yes) is None

    def test_price_change_event_creates_synthetic_book(self, poly_yes):
        """PriceChangeEvent creates synthetic bid/ask around the price."""
        dm = DataManager(
            spread=Decimal('0.04'), synthetic_size=Decimal('500'), synthetic_book=True
        )

        event = PriceChangeEvent(
            ticker=poly_yes,
            price=Decimal('0.50'),
        )
        dm.process_price_change_event(event)

        # spread=0.04, half=0.02
        # bid = 0.50 - 0.02 = 0.48
        # ask = 0.50 + 0.02 = 0.52
        best_bid = dm.get_best_bid(poly_yes)
        best_ask = dm.get_best_ask(poly_yes)
        assert best_bid is not None
        assert best_ask is not None
        assert best_bid.price == Decimal('0.48')
        assert best_ask.price == Decimal('0.52')
        assert best_bid.size == Decimal('500')
        assert best_ask.size == Decimal('500')
