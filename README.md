# PM-CLI: The Agent-First Trading System for Prediction Markets

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/pm-cli.svg)](https://pypi.org/project/pm-cli/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://pre-commit.com/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

**PM-CLI** is an agent-first trading system for prediction markets. It provides a unified framework for building, testing, and deploying autonomous trading agents across [Polymarket](https://polymarket.com/) and [Kalshi](https://kalshi.com/) — the two leading prediction market exchanges — without changing a line of strategy code.

## Why It Works for Both Polymarket and Kalshi

Polymarket and Kalshi operate on fundamentally different rails: Polymarket is a decentralized, crypto-native exchange on Polygon where positions are ERC-1155 tokens settled in USDC, while Kalshi is a regulated US exchange with a traditional order book settled in USD. Despite these differences, both markets share the same core mechanics — binary outcomes, probability-priced contracts, and CLOB-based trading.

PM CLI abstracts over these differences at the exchange layer, exposing a single `Trader` interface to your strategy. Your agent sees the same event stream and issues the same order objects regardless of which venue it is connected to. Swapping exchanges is a one-line change in your run command — your strategy, risk rules, and performance analytics remain identical across both platforms.

## Key Features

- **Exchange-Agnostic Strategies** -- Write one strategy, deploy it on Polymarket or Kalshi without modification
- **Agent-First Design** -- Strategies are async event handlers; the engine drives the loop and your agent just reacts
- **LLM-Powered Decision Making** -- Plug in Large Language Models to analyze news and generate trading signals
- **Multi-Source Data Ingestion** -- Live exchange feeds, News API, and RSS feeds (WSJ, etc.)
- **Risk Management** -- Configurable limits on trade size, position size, drawdown, and daily loss
- **Backtesting** -- Replay historical data through any strategy before going live
- **Paper Trading** -- Simulate live trading on real market data without risking capital
- **Performance Analytics** -- Sharpe ratio, max drawdown, win rate, profit factor, equity curve
- **Operator Controls** -- Real-time monitor UI with pause, resume, and emergency stop over a Unix socket

## Architecture

```
TradingEngine
  ├── DataSource          (Live Exchange / Historical / RSS / News API)
  ├── Strategy            (LLM-based / Custom agent)
  └── Trader              (PaperTrader / PolymarketTrader / KalshiTrader)
       ├── RiskManager    (Conservative / Standard / Aggressive)
       └── PositionManager
```

The engine is the only stateful loop. Strategies are pure event handlers — they receive an `Event`, call methods on the `Trader` interface, and return. Swapping exchanges means swapping the `Trader` and `DataSource` implementations; the strategy and risk layer remain identical.

## Installation

```bash
pip install pm-cli
```

Or install from source with [Poetry](https://python-poetry.org/):

```bash
git clone https://github.com/ulab-uiuc/prediction-market-cli.git
cd prediction-market-cli
pip install poetry
poetry install
```

**Requirements:** Python >= 3.10, < 3.12

## Quick Start

### Run a Backtest

```python
import asyncio
from decimal import Decimal

from pm_cli.core.trading_engine import TradingEngine
from pm_cli.data.backtest.historical_data_source import HistoricalDataSource
from pm_cli.data.market_data_manager import MarketDataManager
from pm_cli.position.position_manager import Position, PositionManager
from pm_cli.risk.risk_manager import NoRiskManager
from pm_cli.strategy.test_strategy import TestStrategy
from pm_cli.ticker.ticker import CashTicker, PolyMarketTicker
from pm_cli.trader.paper_trader import PaperTrader

async def run():
    ticker = PolyMarketTicker(symbol="my_market", name="My Market",
                               market_id="123", event_id="456")
    data_source = HistoricalDataSource("data.jsonl", ticker)
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(ticker=CashTicker.POLYMARKET_USDC, quantity=Decimal("10000"),
                 average_cost=Decimal("0"), realized_pnl=Decimal("0"))
    )
    trader = PaperTrader(market_data=market_data, risk_manager=NoRiskManager(),
                         position_manager=position_manager,
                         min_fill_rate=Decimal("0.5"), max_fill_rate=Decimal("1.0"),
                         commission_rate=Decimal("0.0"))
    engine = TradingEngine(data_source=data_source, strategy=TestStrategy(), trader=trader)
    await engine.start()
    print(f"Final PnL: {position_manager.get_total_realized_pnl()}")

asyncio.run(run())
```

### Live Paper Trading (RSS News)

```python
import asyncio
from decimal import Decimal

from pm_cli.data.live.live_data_source import LiveRSSNewsDataSource
from pm_cli.live.live_trader import run_live_paper_trading
from pm_cli.strategy.test_strategy import TestStrategy

asyncio.run(run_live_paper_trading(
    data_source=LiveRSSNewsDataSource(polling_interval=60.0, max_articles_per_poll=5),
    strategy=TestStrategy(),
    initial_capital=Decimal("10000"),
    duration=300,
))
```

### Custom Strategy

```python
from pm_cli.strategy.strategy import Strategy
from pm_cli.events.events import Event
from pm_cli.trader.trader import Trader

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your agent logic here — same code runs on Polymarket or Kalshi
        pass
```

## CLI

After installation, the `pm-cli` command is available:

```bash
# Strategy scaffolding + validation
pm-cli strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
pm-cli strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Backtest mode
pm-cli backtest run \
  --history-file ./data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Paper trading mode (simulation with live data)
pm-cli paper run --exchange polymarket --strategy-ref ./strategies/my_strategy.py:MyStrategy
pm-cli paper run --exchange kalshi --strategy-ref ./strategies/my_strategy.py:MyStrategy
pm-cli paper run --exchange rss --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Real trading mode
pm-cli live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY"
pm-cli live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH"

# Operator monitor + emergency control
pm-cli monitor
pm-cli trade status
pm-cli trade pause
pm-cli trade resume
pm-cli trade estop
```

### Minimal Commands

```bash
# 1) Minimal backtest
pm-cli backtest run \
  --history-file ./data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref pm_cli.strategy.test_strategy:TestStrategy

# 2) Minimal paper trading (simulation)
pm-cli paper run \
  --exchange polymarket \
  --strategy-ref pm_cli.strategy.test_strategy:TestStrategy

# 3) Minimal live trading (real orders)
pm-cli live run \
  --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref pm_cli.strategy.test_strategy:TestStrategy
```

### Monitor (for human operator)

- Start monitor: `pm-cli monitor`
- Check/pause/resume/stop: `pm-cli trade status|pause|resume|estop`
- Architecture: monitor is a separate UI process, connected to engine via Unix socket (`~/.pm-cli/engine.sock`); closing monitor does not stop the engine.

## Risk Management

Three built-in tiers:

| Setting        | Conservative | Standard (custom) | Aggressive |
| -------------- | ------------ | ----------------- | ---------- |
| Max Trade Size | $500         | configurable      | $5,000     |
| Max Position   | $2,000       | configurable      | $20,000    |
| Max Exposure   | $10,000      | configurable      | $100,000   |
| Max Drawdown   | 10%          | configurable      | 30%        |

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"   # Required for Polymarket live trading
export KALSHI_API_KEY_ID="your_kalshi_key_id"      # Required for Kalshi live trading
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"     # Required for Kalshi live trading
export NEWS_API_KEY="your_news_api_key"             # Optional, for News API source
```

## Development

```bash
poetry install --with dev,test
pre-commit install

# Lint & format
ruff check . && ruff format .

# Type check
mypy pm_cli/

# Test
pytest tests/ -v
```

## License

[Apache 2.0](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

## Disclaimer

This software is for **educational and research purposes only**. Trading involves substantial risk of loss. Always test strategies with paper trading before using real funds.
