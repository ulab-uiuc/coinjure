# SWM Agent: Social World Model Trading Agent

[![Python 3.10](https://img.shields.io/badge/python-3.10-blue.svg)](https://www.python.org/downloads/release/python-3109/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://pre-commit.com/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![bear-ified](https://raw.githubusercontent.com/beartype/beartype-assets/main/badge/bear-ified.svg)](https://beartype.readthedocs.io)

> [!NOTE]
> This repository is continuously updating with more features and improvements. Any contribution is welcome.

## 🎯 Project Overview

**SWM Agent** is an intelligent trading agent designed for Polymarket prediction markets, built on the concept of Social World Models. The agent combines real-time market data, news sentiment analysis, and machine learning to make automated trading decisions.

### Key Features

- **Real-time Market Integration**: Connects to Polymarket's CLOB (Central Limit Order Book) API for live trading
- **News Sentiment Analysis**: Integrates with news APIs to analyze market-moving events
- **LLM-Powered Decision Making**: Uses Large Language Models to analyze news and make trading decisions
- **Comprehensive Risk Management**: Multiple risk managers with configurable limits
- **Backtesting Framework**: Historical data testing capabilities
- **Paper Trading**: Safe simulation mode for strategy testing
- **Performance Analytics**: Detailed metrics including Sharpe ratio, drawdown, win rate

## 🏗️ Architecture

The project follows a modular, event-driven architecture with clear separation of concerns:

```
SWM Agent
├── Core Trading Engine      # Orchestrates data flow and execution
├── Data Sources             # Live (Polymarket, News, RSS) & Historical
├── Strategy Layer           # LLM-based and custom strategies
├── Risk Management          # Position limits, drawdown controls
├── Position Management      # Track holdings and PnL
├── Market Data Processing   # Order book management
└── Analytics                # Performance metrics and reporting
```

## 📁 Project Structure

### Core Components

#### `swm_agent/core/`

- **`trading_engine.py`**: Main orchestration engine that coordinates data flow, strategy execution, and trading operations

#### `swm_agent/strategy/`

- **`strategy.py`**: Abstract base class for all trading strategies
- **`simple_strategy.py`**: LLM-powered strategy that analyzes news events and makes trading decisions
- **`test_strategy.py`**: Testing strategy implementation

#### `swm_agent/trader/`

- **`trader.py`**: Abstract base class for trading interfaces
- **`polymarket_trader.py`**: Concrete implementation for Polymarket CLOB trading
- **`paper_trader.py`**: Simulation trader for testing strategies without real money
- **`types.py`**: Trading-related data types and enums

#### `swm_agent/data/`

- **`data_source.py`**: Abstract base class for data sources
- **`market_data_manager.py`**: Manages market data processing and storage
- **`backtest/`**: Historical data sources for backtesting
- **`live/`**: Real-time data sources for live trading
  - `LivePolyMarketDataSource`: Real-time Polymarket data
  - `LiveNewsDataSource`: News API integration
  - `LiveRSSNewsDataSource`: RSS feed integration (WSJ, etc.)

#### `swm_agent/events/`

- **`events.py`**: Event system for market data and news events
  - `OrderBookEvent`: Market order book updates
  - `NewsEvent`: News articles and sentiment data
  - `PriceChangeEvent`: Price movement events

#### `swm_agent/analytics/`

- **`performance_analyzer.py`**: Trading performance analysis and metrics

#### `swm_agent/backtest/`

- **`backtester.py`**: Historical strategy testing framework

#### `swm_agent/risk/`

- **`risk_manager.py`**: Risk management implementations
  - `NoRiskManager`: No restrictions (for testing)
  - `StandardRiskManager`: Configurable risk limits
  - `ConservativeRiskManager`: Tight limits preset
  - `AggressiveRiskManager`: Looser limits preset

#### `swm_agent/position/`

- **`position_manager.py`**: Position tracking and management

#### `swm_agent/order/`

- **`order_book.py`**: Order book management and analysis

#### `swm_agent/ticker/`

- **`ticker.py`**: Market ticker and symbol management

#### `swm_agent/live/`

- **`live_trader.py`**: Live trading execution engine

### Examples

#### `examples/`

- **`backtest_example.py`**: How to run backtests with historical data
- **`live_paper_trading_example.py`**: Live paper trading with RSS news
- **`custom_strategy_example.py`**: Creating custom strategies (momentum, mean reversion, news keyword)
- **`performance_analysis_example.py`**: Using the performance analyzer

### Scripts

#### `scripts/`

- **`get_live_news_data.py`**: Script to test and collect live news data
- **`get_live_polymarket_data.py`**: Script to test and collect live Polymarket data

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Polymarket API credentials
- News API key (optional, for news sentiment analysis)

### Installation

```bash
# Clone the repository
git clone https://github.com/ulab-uiuc/swm-agent.git
cd swm-agent

# Install dependencies using Poetry
pip install poetry
poetry install

# Or using pip
pip install -e .
```

### Environment Setup

```bash
# Set up environment variables
export POLYMARKET_PRIVATE_KEY="your_private_key"
export NEWS_API_KEY="your_news_api_key"  # Optional
```

## 📖 Usage Guide

### Running a Backtest

```python
import asyncio
from decimal import Decimal

from swm_agent.core.trading_engine import TradingEngine
from swm_agent.data.backtest.historical_data_source import HistoricalDataSource
from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import NoRiskManager
from swm_agent.strategy.test_strategy import TestStrategy
from swm_agent.ticker.ticker import CashTicker, PolyMarketTicker
from swm_agent.trader.paper_trader import PaperTrader

async def run_backtest():
    # Create ticker
    ticker = PolyMarketTicker(
        symbol='my_market',
        name='My Market',
        market_id='123',
        event_id='456',
    )

    # Setup components
    data_source = HistoricalDataSource('data.jsonl', ticker)
    market_data = MarketDataManager()
    position_manager = PositionManager()

    # Initialize with $10,000
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Create trader
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Create engine and run
    engine = TradingEngine(
        data_source=data_source,
        strategy=TestStrategy(),
        trader=trader,
    )

    await engine.start()
    print(f'Final PnL: {position_manager.get_total_realized_pnl()}')

asyncio.run(run_backtest())
```

### Live Paper Trading

```python
import asyncio
from decimal import Decimal

from swm_agent.data.live.live_data_source import LiveRSSNewsDataSource
from swm_agent.live.live_trader import run_live_paper_trading
from swm_agent.strategy.test_strategy import TestStrategy

async def main():
    # Create RSS news data source
    data_source = LiveRSSNewsDataSource(
        polling_interval=60.0,  # Poll every 60 seconds
        max_articles_per_poll=5,
        categories=['finance', 'business'],
    )

    # Run paper trading for 5 minutes
    await run_live_paper_trading(
        data_source=data_source,
        strategy=TestStrategy(),
        initial_capital=Decimal('10000'),
        duration=300,  # 5 minutes
    )

asyncio.run(main())
```

### Using Risk Management

```python
from decimal import Decimal

from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import PositionManager
from swm_agent.risk.risk_manager import StandardRiskManager

# Create risk manager with custom limits
risk_manager = StandardRiskManager(
    position_manager=position_manager,
    market_data=market_data,
    max_single_trade_size=Decimal('500'),    # Max $500 per trade
    max_position_size=Decimal('2000'),        # Max $2000 per ticker
    max_total_exposure=Decimal('8000'),       # Max 80% of capital
    max_drawdown_pct=Decimal('0.15'),         # Stop at 15% drawdown
    daily_loss_limit=Decimal('500'),          # Max $500 daily loss
    max_positions=5,                          # Max 5 open positions
    initial_capital=Decimal('10000'),
)

# Or use presets
from swm_agent.risk.risk_manager import ConservativeRiskManager, AggressiveRiskManager

conservative = ConservativeRiskManager(position_manager, market_data)
aggressive = AggressiveRiskManager(position_manager, market_data)
```

### Performance Analysis

```python
from decimal import Decimal

from swm_agent.analytics.performance_analyzer import PerformanceAnalyzer
from swm_agent.ticker.ticker import PolyMarketTicker
from swm_agent.trader.types import Trade, TradeSide

# Create analyzer
analyzer = PerformanceAnalyzer(initial_capital=Decimal('10000'))

# Add trades
ticker = PolyMarketTicker(symbol='TEST', name='Test')
analyzer.add_trade(Trade(
    side=TradeSide.BUY,
    ticker=ticker,
    price=Decimal('0.50'),
    quantity=Decimal('100'),
    commission=Decimal('0.50'),
))
analyzer.add_trade(Trade(
    side=TradeSide.SELL,
    ticker=ticker,
    price=Decimal('0.60'),
    quantity=Decimal('100'),
    commission=Decimal('0.60'),
))

# Get statistics
stats = analyzer.get_stats()
print(f'Win Rate: {stats.win_rate * 100:.1f}%')
print(f'Sharpe Ratio: {stats.sharpe_ratio:.4f}')
print(f'Max Drawdown: {stats.max_drawdown * 100:.2f}%')
print(f'Profit Factor: {stats.profit_factor:.2f}')

# Print full summary
analyzer.print_summary()

# Access equity curve
curve = analyzer.get_equity_curve()
for point in curve:
    print(f'Trade {point.trade_index}: ${point.equity:,.2f}')
```

### Creating Custom Strategies

```python
from decimal import Decimal

from swm_agent.events.events import Event, NewsEvent, PriceChangeEvent
from swm_agent.strategy.strategy import Strategy
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import TradeSide


class MomentumStrategy(Strategy):
    """Buy when price goes up, sell when it goes down."""

    def __init__(self, threshold: Decimal = Decimal('0.02')):
        self.threshold = threshold
        self.last_prices = {}

    async def process_event(self, event: Event, trader: Trader) -> None:
        if not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        price = event.price
        symbol = ticker.symbol

        if symbol in self.last_prices:
            change = (price - self.last_prices[symbol]) / self.last_prices[symbol]

            if change > self.threshold:
                # Price up -> buy
                await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=price + Decimal('0.01'),
                    quantity=Decimal('100'),
                )
            elif change < -self.threshold:
                # Price down -> sell if we have position
                position = trader.position_manager.get_position(ticker)
                if position and position.quantity > 0:
                    await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=price - Decimal('0.01'),
                        quantity=position.quantity,
                    )

        self.last_prices[symbol] = price
```

### CLI Monitoring

Monitor your trading activities in real-time with the built-in CLI dashboard:

```bash
# Display current portfolio snapshot
swm-agent monitor

# Live monitoring with auto-refresh
swm-agent monitor --watch

# Custom refresh rate (1 second)
swm-agent monitor --watch --refresh 1.0

# Run the demo
python examples/demo_monitor.py --watch
```

The monitor displays:

- **Portfolio Summary**: Total value, cash positions, realized/unrealized P&L
- **Active Positions**: Open positions with current prices and P&L breakdown
- **Recent Orders**: Order history with status and fill details
- **Market Snapshot**: Real-time bid/ask spreads
- **Statistics**: Success rate, order counts, session runtime

For detailed usage and integration instructions, see [CLI Monitoring Documentation](docs/CLI_MONITORING.md).

## 🔧 Configuration

The agent supports various configuration options:

### Trading Parameters

- Position sizes
- Confidence thresholds
- Order types (FOK, etc.)

### Risk Management

- Maximum trade size
- Maximum position size per ticker
- Maximum total portfolio exposure
- Maximum drawdown percentage
- Daily loss limits
- Maximum number of open positions

### Data Sources

- API endpoints
- Polling intervals
- Article limits per poll
- Category filters

### LLM Configuration

- Model selection
- API keys
- Temperature settings

## 📊 Strategy Overview

### Simple Strategy (LLM-Powered)

The default strategy uses Large Language Models to:

1. **Analyze News Events**: Process incoming news articles for market sentiment
2. **Generate Trading Signals**: Determine buy/sell/hold decisions with confidence scores
3. **Execute Trades**: Place orders based on LLM analysis when confidence exceeds threshold

### Test Strategy

A simple momentum-following strategy for testing:

- Buys when price increases
- Sells when price decreases
- Fixed position sizing

### Custom Strategies

Implement custom strategies by extending the `Strategy` base class:

```python
from swm_agent.strategy.strategy import Strategy
from swm_agent.events.events import Event
from swm_agent.trader.trader import Trader

class MyCustomStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your custom logic here
        pass
```

## 🛡️ Risk Management

The agent includes comprehensive risk management with three tiers:

### NoRiskManager

- No restrictions
- Useful for testing

### StandardRiskManager

Configurable limits for:

- **Trade Size**: Maximum value for a single trade
- **Position Size**: Maximum position value per ticker
- **Total Exposure**: Maximum total portfolio market exposure
- **Drawdown**: Maximum percentage loss from peak
- **Daily Loss**: Maximum daily loss allowed
- **Position Count**: Maximum number of open positions

### Preset Risk Managers

| Setting            | Conservative | Aggressive |
| ------------------ | ------------ | ---------- |
| Max Trade Size     | $500         | $5,000     |
| Max Position Size  | $2,000       | $20,000    |
| Max Total Exposure | $10,000      | $100,000   |
| Max Drawdown       | 10%          | 30%        |
| Daily Loss Limit   | $500         | None       |
| Max Positions      | 5            | 20         |

## 📈 Performance Analytics

Track trading performance with built-in analytics:

### Metrics Available

- **Total PnL**: Total profit/loss
- **Win Rate**: Percentage of winning trades
- **Sharpe Ratio**: Risk-adjusted returns (annualized)
- **Max Drawdown**: Maximum peak-to-trough decline
- **Profit Factor**: Gross profit / gross loss
- **Average Profit/Loss**: Per winning/losing trade
- **Consecutive Streaks**: Max consecutive wins/losses

### Equity Curve

Track portfolio value over time with detailed equity curve analysis.

## 🧪 Testing

### Running Unit Tests

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run specific test file
pytest tests/test_position_manager.py

# Run with coverage report
pytest tests/ --cov=swm_agent --cov-report=html
```

### Test Coverage

The test suite covers all major components:

- Position Manager (11 tests)
- Order Book (13 tests)
- Market Data Manager (12 tests)
- Risk Manager (14 tests)
- Performance Analyzer (15 tests)
- Paper Trader (15 tests)
- Events (17 tests)
- Trading Engine (8 tests)
- Ticker (15 tests)

### Running Examples

```bash
# Run backtest example
python examples/backtest_example.py

# Run live paper trading example
python examples/live_paper_trading_example.py

# Run custom strategy example
python examples/custom_strategy_example.py

# Run performance analysis example
python examples/performance_analysis_example.py
```

### Testing Data Sources

```bash
# Test news data collection
python scripts/get_live_news_data.py --api-token YOUR_TOKEN --duration 300

# Test Polymarket data collection
python scripts/get_live_polymarket_data.py --duration 300
```

## 📚 API Reference

### Key Classes

#### TradingEngine

```python
TradingEngine(data_source, strategy, trader)
await engine.start()  # Start processing events
engine.stop()         # Stop the engine
```

#### PositionManager

```python
pm = PositionManager()
pm.update_position(position)           # Update a position
pm.apply_trade(trade)                  # Apply a trade
pm.get_position(ticker)                # Get position for ticker
pm.get_cash_positions()                # Get all cash positions
pm.get_non_cash_positions()            # Get all market positions
pm.get_total_realized_pnl()            # Total realized PnL
pm.get_portfolio_value(market_data)    # Total portfolio value
```

#### PaperTrader

```python
trader = PaperTrader(market_data, risk_manager, position_manager, ...)
result = await trader.place_order(side, ticker, limit_price, quantity)
# result.order - the filled order or None
# result.failure_reason - why it failed (if applicable)
```

#### PerformanceAnalyzer

```python
analyzer = PerformanceAnalyzer(initial_capital)
analyzer.add_trade(trade)           # Add a trade
stats = analyzer.get_stats()        # Get statistics
curve = analyzer.get_equity_curve() # Get equity curve
analyzer.print_summary()            # Print formatted summary
analyzer.reset()                    # Reset to initial state
```

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details.

### Development Setup

```bash
# Install development dependencies
poetry install --with dev

# Set up pre-commit hooks
pre-commit install

# Run linting
ruff check .
ruff format .

# Run type checking
mypy swm_agent/
```

## 📄 License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This software is for educational and research purposes. Trading involves substantial risk of loss and is not suitable for all investors. Past performance does not guarantee future results. Always test strategies thoroughly with paper trading before using real funds.
