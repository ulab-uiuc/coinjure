![Pred Market CLI](assets/pm-cli.png)

<h1 align="center">
  Agent-First Trading System for Prediction Markets
</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/pm-cli/"><img src="https://img.shields.io/pypi/v/pm-cli.svg" alt="PyPI version"></a>
  <a href="https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

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

AI agents are now strong enough to self-discover alpha from messy, fast-moving market and news streams. The practical question is no longer “can an agent reason about trades,” but:

Can we build the tools, infrastructure, and environment that let an agent operate like a disciplined human trader, but through command-line APIs that are easy for agents to use?

PM-CLI is our answer:

- Strategy is the primary interface.
- The engine owns execution, risk checks, and lifecycle.
- Data, simulation, monitoring, and live controls are unified behind CLI commands.

This lets agents iterate quickly, safely, and reproducibly without rebuilding trading plumbing for every strategy or exchange.

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
pm-cli news fetch --source rss --query "fed rates" --limit 10
```

### 2) Scaffold and validate a strategy

```bash
pm-cli strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
pm-cli strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy
```

Use constructor kwargs when needed:

```bash
pm-cli strategy validate \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25", "entry_imbalance": 0.35}'
```

Quick runtime smoke check before backtest/paper run:

```bash
pm-cli strategy dry-run \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --events 10 --json
```

Built-in example strategy files (good templates for agents):

```bash
pm-cli strategy validate \
  --strategy-ref ./examples/strategies/threshold_momentum_strategy.py:ThresholdMomentumStrategy

pm-cli strategy validate \
  --strategy-ref ./examples/strategies/orderbook_pressure_strategy.py:OrderBookPressureStrategy
```

### 3) Run paper trading with monitor

```bash
pm-cli paper run \
  --exchange polymarket \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --monitor
```

### 4) Operator control commands (separate terminal)

```bash
pm-cli trade status
pm-cli trade pause
pm-cli trade resume
pm-cli trade stop
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

### 6) Agent strategy-discovery toolkit (`research`)

Use these composable tools when an agent needs to discover strategies on yes/no time-series:

```bash
# 1) build a clean per-market slice
pm-cli research slice \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --output ./data/m1_e1_slice.jsonl

# 2) build features + labels
pm-cli research features \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --output ./data/m1_e1_features.jsonl

pm-cli research labels \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --horizon-steps 5 \
  --threshold 0.01 \
  --output ./data/m1_e1_labels.jsonl

# 3) run parameter sweeps, rank, and persist memory
pm-cli research backtest-batch \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --params-jsonl ./data/params.jsonl \
  --output ./data/batch_runs.jsonl

pm-cli research compare-runs \
  --input-file ./data/batch_runs.jsonl \
  --sort-key sharpe_ratio \
  --top 20 \
  --output ./data/top_runs.jsonl

pm-cli research memory add \
  --input-file ./data/top_runs.jsonl \
  --tag m1_e1
```

Also available: `research universe`, `research walk-forward`, `research stress-test`, `research strategy-gate`, and `research memory list`.

## Human-in-the-Loop Model

The operator is intentionally lightweight:

- Use `pm-cli monitor` for live visibility.
- Use `pm-cli trade pause|resume|stop` for intervention.

The operator should not need to manually place/cancel orders in normal operation.

## CLI Reference

Primary command groups:

- `pm-cli strategy`: create and validate strategies.
- `pm-cli paper`: paper trading with live feeds.
- `pm-cli backtest`: historical replay.
- `pm-cli monitor`: attach monitor UI to running engine.
- `pm-cli trade`: runtime control (`status`, `pause`, `resume`, `stop`, `killswitch`).
- `pm-cli market`: market discovery and metadata.
- `pm-cli news`: standalone news fetching.
- `pm-cli data`: live event recording.
- `pm-cli research`: strategy-discovery tools (slice/features/labels/batch/walk-forward/stress/gate/memory).

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
