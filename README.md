![Pred Market CLI](assets/pm-cli.png)

<h1 align="center">PM-CLI: Agent-First Trading System for Prediction Markets</h1>

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/pm-cli.svg)](https://pypi.org/project/pm-cli/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

PM-CLI is an agent-first trading stack for prediction markets.

The goal is simple: give an autonomous agent everything it needs to design, test, and run strategies end-to-end, while the human operator only does two things:

1. Monitor the system.
2. Pause or emergency-stop when needed.

## 1-Minute Start

```bash
pm-cli paper run --exchange polymarket --strategy-ref pm_cli.strategy.orderbook_imbalance_strategy:OrderBookImbalanceStrategy

pm-cli monitor
```

![PM-CLI Monitor](assets/pm_cli_monitor.png)

## Why Agent-First

Most trading tools are built for manual operators and bolt on automation later. PM-CLI is the reverse:

- Strategy is the primary interface.
- The engine owns execution, risk checks, and lifecycle.
- Data, simulation, and live controls are available from one CLI.

This lets agents iterate quickly without rewriting plumbing for every strategy or exchange.

## What We Provide for Agent Trading

PM-CLI provides the full toolchain needed by an autonomous trading agent:

- Unified strategy API across Polymarket and Kalshi.
- Live market + news ingestion in paper mode.
- Built-in paper trading execution engine.
- Backtest replay for regression checks.
- Runtime monitor and control socket.
- Pause/resume/stop controls for human override.

In paper mode, `--exchange polymarket` and `--exchange kalshi` run with a composite source (market feed + Google News + RSS), with conservative news polling defaults.

## System Structure

Core runtime components:

- `DataSource`: live market/news or historical replay.
- `Strategy`: async decision logic (`process_event`).
- `Trader`: execution adapter (`PaperTrader`, `PolymarketTrader`, `KalshiTrader`).
- `RiskManager`: pre-trade constraints and safety checks.
- `PositionManager`: positions, realized/unrealized PnL.
- `TradingEngine`: event loop, orchestration, monitoring state.
- `ControlServer`: pause/resume/status/stop via Unix socket.
- `Monitor UI`: human visibility and emergency controls.

```text
TradingEngine
  |- DataSource (market/news/backtest)
  |- Strategy (agent logic)
  |- Trader (paper/live exchange adapter)
  |  |- RiskManager
  |  |- PositionManager
  |- ControlServer (socket)
  |- Monitor UI (operator)
```

## Installation

```bash
pip install pm-cli
```

From source:

```bash
git clone https://github.com/ulab-uiuc/prediction-market-cli.git
cd prediction-market-cli
pip install poetry
poetry install
```

## Quick Start (CLI Only)

### 1) Explore market and news inputs

```bash
pm-cli market list --exchange polymarket --limit 20
pm-cli market search --exchange polymarket --query "election" --limit 20
pm-cli market info --exchange polymarket --market-id <market_id>

pm-cli news fetch --source google --query "prediction market" --limit 10
pm-cli news search --source rss --query "fed rates" --limit 10
```

### 2) Scaffold and validate a strategy

```bash
pm-cli strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
pm-cli strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy
```

### 3) Run paper trading with monitor

```bash
pm-cli paper run \
  --exchange polymarket \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --monitor
```

### 4) Operator control commands (separate terminal)

```bash
pm-cli trade status
pm-cli trade pause
pm-cli trade resume
pm-cli trade estop
```

### 5) Record and backtest

```bash
pm-cli data record --exchange polymarket --output ./data/events.jsonl --duration 300

pm-cli backtest run \
  --history-file ./data/events.jsonl \
  --market-id M1 \
  --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy
```

## Human-in-the-Loop Model

The operator is intentionally lightweight:

- Use `pm-cli monitor` for live visibility.
- Use `pm-cli trade pause|resume|estop` for intervention.

The operator should not need to manually place/cancel orders in normal operation.

## CLI Reference

Primary command groups:

- `pm-cli strategy`: create and validate strategies.
- `pm-cli paper`: paper trading with live feeds.
- `pm-cli backtest`: historical replay.
- `pm-cli monitor`: attach monitor UI to running engine.
- `pm-cli trade`: runtime control (`status`, `pause`, `resume`, `stop`, `estop`).
- `pm-cli market`: market discovery and metadata.
- `pm-cli news`: standalone news fetching.
- `pm-cli data`: live event recording.

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"
export KALSHI_API_KEY_ID="your_kalshi_key_id"
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"
```

## Development

```bash
poetry install --with dev,test
pre-commit install
ruff check . && ruff format .
mypy pm_cli/
pytest tests/ -v
```

## License

[MIT](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

## Disclaimer

This software is for educational and research use. Live trading carries financial risk.
