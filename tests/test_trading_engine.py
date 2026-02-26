from decimal import Decimal

import pytest

from coinjure.core.trading_engine import TradingEngine
from coinjure.data.data_source import DataSource
from coinjure.data.market_data_manager import MarketDataManager
from coinjure.events.events import (
    Event,
    NewsEvent,
    OrderBookEvent,
    PriceChangeEvent,
)
from coinjure.order.order_book import Level, OrderBook
from coinjure.position.position_manager import Position, PositionManager
from coinjure.risk.risk_manager import NoRiskManager
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import CashTicker, PolyMarketTicker
from coinjure.trader.paper_trader import PaperTrader
from coinjure.trader.trader import Trader


@pytest.fixture
def test_ticker() -> PolyMarketTicker:
    """Create a test ticker."""
    return PolyMarketTicker(
        symbol='TEST_TOKEN',
        name='Test Market',
        token_id='token123',
        market_id='market123',
        event_id='event123',
    )


class MockDataSource(DataSource):
    """Mock data source that returns predefined events."""

    def __init__(self, events: list[Event]):
        self.events = events
        self.index = 0

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None


class MockStrategy(Strategy):
    """Mock strategy that tracks processed events."""

    def __init__(self):
        self.processed_events: list[Event] = []

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.processed_events.append(event)


@pytest.fixture
def market_data(test_ticker: PolyMarketTicker) -> MarketDataManager:
    """Create market data manager."""
    mdm = MarketDataManager()
    order_book = OrderBook()
    order_book.update(
        asks=[Level(price=Decimal('0.55'), size=Decimal('1000'))],
        bids=[Level(price=Decimal('0.50'), size=Decimal('1000'))],
    )
    mdm.order_books[test_ticker] = order_book
    return mdm


@pytest.fixture
def position_manager() -> PositionManager:
    """Create position manager."""
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
    """Create a paper trader."""
    return PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0'),
    )


class TestTradingEngine:
    @pytest.mark.asyncio
    async def test_engine_creation(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test creating a trading engine."""
        data_source = MockDataSource([])
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        assert engine.data_source == data_source
        assert engine.strategy == strategy
        assert engine.trader == paper_trader
        assert engine.running is False

    @pytest.mark.asyncio
    async def test_engine_processes_events(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that engine processes all events."""
        events = [
            NewsEvent(news='Test news 1'),
            NewsEvent(news='Test news 2'),
            NewsEvent(news='Test news 3'),
        ]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        assert len(strategy.processed_events) == 3
        assert engine.running is False

    @pytest.mark.asyncio
    async def test_engine_stops_on_none(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test that engine stops when data source returns None."""
        events = [NewsEvent(news='Test news')]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        assert engine.running is False

    @pytest.mark.asyncio
    async def test_engine_processes_orderbook_events(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        market_data: MarketDataManager,
    ):
        """Test that OrderBookEvents are processed by market data manager."""
        events = [
            OrderBookEvent(
                ticker=test_ticker,
                price=Decimal('0.52'),
                size=Decimal('500'),
                size_delta=Decimal('500'),
            ),
        ]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        # Event should be processed by strategy
        assert len(strategy.processed_events) == 1
        assert isinstance(strategy.processed_events[0], OrderBookEvent)

    @pytest.mark.asyncio
    async def test_engine_processes_price_change_events(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        market_data: MarketDataManager,
    ):
        """Test that PriceChangeEvents update market data."""
        events = [
            PriceChangeEvent(
                ticker=test_ticker,
                price=Decimal('0.60'),
            ),
        ]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        # Event should be processed by strategy
        assert len(strategy.processed_events) == 1
        assert isinstance(strategy.processed_events[0], PriceChangeEvent)

        # Market data should be updated with synthetic order book
        assert test_ticker in market_data.order_books

    @pytest.mark.asyncio
    async def test_engine_stop(
        self, paper_trader: PaperTrader, test_ticker: PolyMarketTicker
    ):
        """Test stopping the engine."""
        events = [NewsEvent(news='Test news')]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.stop()
        assert engine.running is False

    @pytest.mark.asyncio
    async def test_engine_processes_mixed_events(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
    ):
        """Test processing a mix of event types."""
        events = [
            NewsEvent(news='Breaking news'),
            PriceChangeEvent(ticker=test_ticker, price=Decimal('0.55')),
            OrderBookEvent(
                ticker=test_ticker,
                price=Decimal('0.52'),
                size=Decimal('500'),
                size_delta=Decimal('500'),
            ),
            NewsEvent(news='More news'),
        ]

        data_source = MockDataSource(events)
        strategy = MockStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        assert len(strategy.processed_events) == 4
        assert isinstance(strategy.processed_events[0], NewsEvent)
        assert isinstance(strategy.processed_events[1], PriceChangeEvent)
        assert isinstance(strategy.processed_events[2], OrderBookEvent)
        assert isinstance(strategy.processed_events[3], NewsEvent)


class TestTradingEngineWithRealStrategy:
    @pytest.mark.asyncio
    async def test_engine_with_test_strategy(
        self,
        paper_trader: PaperTrader,
        test_ticker: PolyMarketTicker,
        market_data: MarketDataManager,
    ):
        """Test engine with the actual TestStrategy."""
        from coinjure.strategy.test_strategy import TestStrategy

        # Create price change events that trigger the strategy
        events = [
            PriceChangeEvent(ticker=test_ticker, price=Decimal('0.50')),
            PriceChangeEvent(
                ticker=test_ticker, price=Decimal('0.55')
            ),  # Price up -> buy
        ]

        data_source = MockDataSource(events)
        strategy = TestStrategy()

        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=paper_trader,
        )

        await engine.start()

        # Strategy should have recorded the last price
        assert test_ticker in strategy.last_prices
