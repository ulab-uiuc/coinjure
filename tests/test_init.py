"""
SWM Agent Test Suite

This package contains unit tests for all components of the SWM Agent framework.

Test modules:
- test_position_manager: Tests for position tracking and PnL calculation
- test_order_book: Tests for order book data structures
- test_market_data_manager: Tests for market data processing
- test_risk_manager: Tests for risk management implementations
- test_performance_analyzer: Tests for performance metrics calculation
- test_paper_trader: Tests for simulated trading
- test_events: Tests for event types (News, OrderBook, PriceChange)
- test_trading_engine: Tests for the main trading engine
- test_ticker: Tests for ticker types

Run all tests with: pytest tests/
Run with coverage: pytest tests/ --cov=swm_agent --cov-report=html
"""


def test_package_imports() -> None:
    """Test that main package components can be imported."""
    # Core components
    # Analytics
    from swm_agent.analytics.performance_analyzer import PerformanceAnalyzer
    from swm_agent.core.trading_engine import TradingEngine

    # Data components
    from swm_agent.data.data_source import DataSource
    from swm_agent.data.market_data_manager import MarketDataManager

    # Events
    from swm_agent.events.events import NewsEvent, OrderBookEvent, PriceChangeEvent

    # Position management
    from swm_agent.position.position_manager import Position, PositionManager

    # Risk management
    from swm_agent.risk.risk_manager import (
        NoRiskManager,
        RiskManager,
        StandardRiskManager,
    )

    # Strategy
    from swm_agent.strategy.strategy import Strategy

    # Tickers
    from swm_agent.ticker.ticker import CashTicker, PolyMarketTicker

    # Traders
    from swm_agent.trader.paper_trader import PaperTrader
    from swm_agent.trader.types import Order, Trade, TradeSide

    # All imports successful
    assert True
