# SWM Agent: Social World Model Trading Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/swm-agent.svg)](https://pypi.org/project/swm-agent/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/ulab-uiuc/swm-agent/blob/main/LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://pre-commit.com/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

**SWM Agent** is an intelligent trading agent for [Polymarket](https://polymarket.com/) prediction markets, powered by Social World Models and LLM-driven decision making. It combines real-time market data, news sentiment analysis, and Large Language Models to automate trading decisions.

## Key Features

- **Real-time Market Integration** -- Connects to Polymarket's CLOB API for live order book data and trading
- **LLM-Powered Strategies** -- Analyzes news events with Large Language Models to generate trading signals
- **Multi-Source Data Ingestion** -- Polymarket live data, News API, and RSS feeds (WSJ, etc.)
- **Risk Management** -- Configurable limits on trade size, position size, drawdown, daily loss, and more
- **Backtesting** -- Test strategies against historical data before going live
- **Paper Trading** -- Simulate live trading without real capital
- **Performance Analytics** -- Sharpe ratio, max drawdown, win rate, profit factor, equity curve

## Architecture

```
TradingEngine
  ├── DataSource          (Live / Historical / RSS / News API)
  ├── Strategy            (LLM-based / Custom)
  └── Trader              (PaperTrader / PolymarketTrader)
       ├── RiskManager    (Standard / Conservative / Aggressive)
       └── PositionManager
```

## Installation

```bash
pip install swm-agent
```

Or install from source with [Poetry](https://python-poetry.org/):

```bash
git clone https://github.com/ulab-uiuc/swm-agent.git
cd swm-agent
pip install poetry
poetry install
```

**Requirements:** Python >= 3.10, < 3.12

## Quick Start

### Run a Backtest

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

from swm_agent.data.live.live_data_source import LiveRSSNewsDataSource
from swm_agent.live.live_trader import run_live_paper_trading
from swm_agent.strategy.test_strategy import TestStrategy

asyncio.run(run_live_paper_trading(
    data_source=LiveRSSNewsDataSource(polling_interval=60.0, max_articles_per_poll=5),
    strategy=TestStrategy(),
    initial_capital=Decimal("10000"),
    duration=300,
))
```

### Custom Strategy

```python
from swm_agent.strategy.strategy import Strategy
from swm_agent.events.events import Event
from swm_agent.trader.trader import Trader

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your logic here
        pass

    # Optional: respect control-plane pause/resume.
    # if self.is_paused():
    #     return
```

## CLI

After installation, the `swm-agent` command is available:

```bash
# Strategy scaffolding + validation
swm-agent strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
swm-agent strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Backtest mode
swm-agent backtest run \
  --history-file ./data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Paper trading mode (simulation with live data)
swm-agent paper run --exchange polymarket --strategy-ref ./strategies/my_strategy.py:MyStrategy
swm-agent paper run --exchange kalshi --strategy-ref ./strategies/my_strategy.py:MyStrategy
swm-agent paper run --exchange rss --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Real trading mode
swm-agent live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY"
swm-agent live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH"

# Operator monitor + emergency control
swm-agent monitor
swm-agent trade status
swm-agent trade pause
swm-agent trade resume
swm-agent trade estop
```

### Minimal Commands

```bash
# 1) Minimal backtest
swm-agent backtest run \
  --history-file ./data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref swm_agent.strategy.test_strategy:TestStrategy

# 2) Minimal paper trading (simulation)
swm-agent paper run \
  --exchange polymarket \
  --strategy-ref swm_agent.strategy.test_strategy:TestStrategy

# 3) Minimal live trading (real orders)
swm-agent live run \
  --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref swm_agent.strategy.test_strategy:TestStrategy
```

### Monitor (for human operator)

- Start monitor: `swm-agent monitor`
- Check/pause/resume/stop: `swm-agent trade status|pause|resume|estop`
- Architecture: monitor is a separate UI process, connected to engine via Unix socket (`~/.swm/engine.sock`); closing monitor does not stop engine.

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
export POLYMARKET_PRIVATE_KEY="your_private_key"   # Required for live trading
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
mypy swm_agent/

# Test
pytest tests/ -v
```

## License

[Apache 2.0](https://github.com/ulab-uiuc/swm-agent/blob/main/LICENSE)

## Disclaimer

This software is for **educational and research purposes only**. Trading involves substantial risk of loss. Always test strategies with paper trading before using real funds.
