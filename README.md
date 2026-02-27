![Coinjure](assets/coinjure.png)

<h1 align="center">
  Agent-Native Trading System for Prediction Markets
</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/coinjure/"><img src="https://img.shields.io/pypi/v/coinjure.svg" alt="PyPI version"></a>
  <a href="https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

Coinjure is an agent-native trading stack for prediction markets.

The goal is simple: give an autonomous agent everything it needs to design, test, and run strategies end-to-end, while the human operator only does two things:

1. Monitor the system.
2. Pause or emergency-stop when needed.

## 1-Minute Start

```bash
coinjure paper run --exchange polymarket --strategy-ref coinjure.strategy.orderbook_imbalance_strategy:OrderBookImbalanceStrategy

coinjure monitor
```

![Coinjure Monitor](assets/coinjure_monitor.png)

## Why Agent-First

AI agents are now strong enough to self-discover alpha from messy, fast-moving market and news streams. The practical question is no longer “can an agent reason about trades,” but:

Can we build the tools, infrastructure, and environment that let an agent operate like a disciplined human trader, but through command-line APIs that are easy for agents to use?

Coinjure is our answer:

- Strategy is the primary interface.
- The engine owns execution, risk checks, and lifecycle.
- Data, simulation, monitoring, and live controls are unified behind CLI commands.

This lets agents iterate quickly, safely, and reproducibly without rebuilding trading plumbing for every strategy or exchange.

## What We Provide for Agent Trading

Coinjure provides the full toolchain needed by an autonomous trading agent:

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
pip install coinjure
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
coinjure market list --exchange polymarket --limit 20
coinjure market search --exchange polymarket --query "election" --limit 20
coinjure market info --exchange polymarket --market-id <market_id>

coinjure news fetch --source google --query "prediction market" --limit 10
coinjure news fetch --source rss --query "fed rates" --limit 10
```

### 2) Scaffold and validate a strategy

```bash
coinjure strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
coinjure strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy
```

Use constructor kwargs when needed:

```bash
coinjure strategy validate \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25", "entry_imbalance": 0.35}'
```

Quick runtime smoke check before backtest/paper run:

```bash
coinjure strategy validate \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --dry-run --events 10 --json
```

Built-in example strategy files (good templates for agents):

```bash
coinjure strategy validate \
  --strategy-ref ./examples/strategies/threshold_momentum_strategy.py:ThresholdMomentumStrategy

coinjure strategy validate \
  --strategy-ref ./examples/strategies/orderbook_pressure_strategy.py:OrderBookPressureStrategy
```

### 3) Run paper trading with monitor

```bash
coinjure paper run \
  --exchange polymarket \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --monitor
```

### 4) Operator control commands (separate terminal)

```bash
coinjure trade status
coinjure trade pause
coinjure trade resume
coinjure trade stop
```

### 5) Record and backtest

```bash
coinjure data record --exchange polymarket --output ./data/events.jsonl --duration 300

# Standard backtest (interactive output)
coinjure backtest run \
  --history-file ./data/events.jsonl \
  --market-id M1 \
  --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Machine-readable JSON output (agent-friendly)
coinjure backtest run \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --json
# → {"ok": true, "total_trades": 12, "win_rate": "0.583", "sharpe_ratio": "1.24", ...}
```

### 6) Agent strategy-discovery toolkit (`research`)

Use these composable tools when an agent needs to discover strategies on yes/no time-series:

```bash
# 1) build a clean per-market slice
# supports UNIX or ISO-8601 timestamps in historical files
coinjure research slice \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --output ./data/m1_e1_slice.jsonl

# 2) build features + labels
coinjure research features \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --output ./data/m1_e1_features.jsonl

coinjure research labels \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --horizon-steps 5 \
  --threshold 0.01 \
  --output ./data/m1_e1_labels.jsonl

# 3) run parameter sweeps, rank, and persist memory
coinjure research backtest-batch \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --params-jsonl ./data/params.jsonl \
  --output ./data/batch_runs.jsonl

coinjure research compare-runs \
  --input-file ./data/batch_runs.jsonl \
  --sort-key sharpe_ratio \
  --top 20 \
  --output ./data/top_runs.jsonl

coinjure research memory add \
  --input-file ./data/top_runs.jsonl \
  --tag m1_e1

# 4) scan many market/event pairs quickly and keep the best run per market
coinjure research scan-markets \
  --history-file ./data/events.jsonl \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --params-jsonl ./data/params.jsonl \
  --max-markets 25 \
  --min-points 30 \
  --output ./data/market_scan.jsonl
```

Supported intervals: `1d` (default), `6h`, `1h`.

## Human-in-the-Loop Model

The operator is intentionally lightweight:

- Use `coinjure monitor` for live visibility.
- Use `coinjure trade pause|resume|stop` for intervention.

The operator should not need to manually place/cancel orders in normal operation.

## CLI Reference

### `coinjure strategy` — Strategy management

| Command | Key options | Description |
|---------|-------------|-------------|
| `strategy create` | `--output` (req), `--class-name`, `--force` | Scaffold a new strategy file |
| `strategy validate` | `--strategy-ref` (req), `--strategy-kwargs-json`, `--dry-run`, `--events`, `--json` | Import-check and optionally feed mock events |

### `coinjure backtest` — Historical replay

| Command | Key options | Description |
|---------|-------------|-------------|
| `backtest run` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref`, `--strategy-kwargs-json`, `--initial-capital`, `--spread`, `--min-fill-rate`, `--max-fill-rate`, `--commission-rate`, `--risk-profile`, `--json` | Run a backtest against a local history file |

### `coinjure paper` — Paper trading

| Command | Key options | Description |
|---------|-------------|-------------|
| `paper run` | `--exchange` (`polymarket`/`kalshi`/`rss`), `--strategy-ref`, `--strategy-kwargs-json`, `--duration`, `--initial-capital`, `--history-file`, `--market-id`, `--event-id`, `--monitor`/`-m`, `--json` | Live-data paper trading with simulated execution |

### `coinjure live` — Live trading

| Command | Key options | Description |
|---------|-------------|-------------|
| `live run` | `--exchange` (req: `polymarket`/`kalshi`), `--strategy-ref`, `--strategy-kwargs-json`, `--duration`, `--wallet-private-key`, `--signature-type`, `--funder`, `--kalshi-api-key-id`, `--kalshi-private-key-path`, `--monitor`/`-m`, `--json` | Real-money live trading |

### `coinjure monitor` — Live UI

| Command | Key options | Description |
|---------|-------------|-------------|
| `monitor` | `--socket`/`-s` | Attach Textual TUI to a running engine |

### `coinjure trade` — Runtime control

| Command | Key options | Description |
|---------|-------------|-------------|
| `trade pause` | `--socket`/`-s`, `--json` | Pause event ingestion and strategy decisions |
| `trade resume` | `--socket`/`-s`, `--json` | Resume after pause |
| `trade status` | `--socket`/`-s`, `--json` | Show engine status (uptime, events, decisions, orders) |
| `trade stop` | `--socket`/`-s`, `--json` | Graceful shutdown |
| `trade swap` | `--strategy-ref` (req), `--strategy-kwargs-json`, `--socket`/`-s`, `--json` | Hot-swap strategy without restarting |
| `trade state` | `--socket`/`-s`, `--json` | Full snapshot (positions, PnL, decisions, order books) |
| `trade killswitch` | `--on`, `--off`, `--path`, `--json` | Toggle/query the global kill-switch file |

### `coinjure market` — Market discovery

| Command | Key options | Description |
|---------|-------------|-------------|
| `market list` | `--exchange`, `--limit`, `--kalshi-api-key-id`, `--kalshi-private-key-path`, `--json` | List open markets |
| `market search` | `--query` (req), `--exchange`, `--limit`, `--json` | Search markets by keyword |
| `market info` | `--market-id` (req), `--exchange`, `--json` | Detailed market metadata |
| `market history` | `--market-id` (req), `--interval` (`1d`/`6h`/`1h`), `--limit`, `--json` | Polymarket price history |

### `coinjure news` — News fetching

| Command | Key options | Description |
|---------|-------------|-------------|
| `news fetch` | `--source` (`google`/`rss`/`thenewsapi`), `--query`, `--limit`, `--api-token`, `--json` | Fetch news articles |

### `coinjure data` — Data recording

| Command | Key options | Description |
|---------|-------------|-------------|
| `data record` | `--exchange`, `--output`, `--duration`, `--polling-interval`, `--kalshi-api-key-id`, `--kalshi-private-key-path`, `--verbose`/`-v`, `--json` | Record live events to a JSONL file |

### `coinjure research` — Strategy discovery toolkit

| Command | Key options | Description |
|---------|-------------|-------------|
| `research markets` | `--history-file` (req), `--sort-by`, `--limit`, `--min-points`, `--min-volume`, `--min-span-seconds`, `--output`, `--json` | Rank markets in a history file |
| `research slice` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--start-ts`, `--end-ts`, `--max-points`, `--output` (req), `--json` | Extract a per-market time slice |
| `research features` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--windows`, `--z-window`, `--output` (req), `--json` | Generate feature matrix |
| `research labels` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--horizon-steps`, `--threshold`, `--output` (req), `--json` | Generate forward-return labels |
| `research backtest-batch` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--params-jsonl`, `--initial-capital`, `--max-runs`, `--output` (req), `--json` | Run strategy over a param list |
| `research grid` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--param-grid-json` (req), `--initial-capital`, `--max-runs`, `--sort-key`, `--output` (req), `--json` | Grid search over hyperparameters |
| `research compare-runs` | `--input-file` (req), `--sort-key`, `--top`, `--output`, `--json` | Rank and filter a set of run results |
| `research scan-markets` | `--history-file` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--params-jsonl`, `--initial-capital`, `--max-markets`, `--min-points`, `--sort-key`, `--output` (req), `--json` | Scan strategy across many markets |
| `research walk-forward` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--train-size`, `--test-size`, `--step-size`, `--initial-capital`, `--output` (req), `--json` | Manual walk-forward validation |
| `research walk-forward-auto` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--min-train-size`, `--min-test-size`, `--target-runs`, `--initial-capital`, `--output` (req), `--json` | Auto-sized walk-forward |
| `research stress-test` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--initial-capital`, `--output` (req), `--json` | Stress scenarios (spread shocks, fill-rate reduction) |
| `research strategy-gate` | `--history-file` (req), `--market-id` (req), `--event-id` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--min-trades`, `--min-total-pnl`, `--max-drawdown-pct`, `--json` | Promotion gate (pass/fail with thresholds) |
| `research alpha-pipeline` | `--history-file` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--market-id`, `--event-id`, `--market-sort-by`, `--market-rank`, `--dry-run-events`, `--initial-capital`, `--min-trades`, `--min-total-pnl`, `--max-drawdown-pct`, `--batch-limit`, `--run-batch-markets`/`--no-run-batch-markets`, `--artifacts-dir`, `--json` | Full pipeline: validate + backtest + stress + gate (+ optional batch) in one shot |
| `research batch-markets` | `--history-file` (req), `--strategy-ref` (req), `--strategy-kwargs-json`, `--initial-capital`, `--limit`, `--output` (req), `--json` | Run one strategy across N markets |
| `research memory add` | `--input-file` (req), `--memory-file`, `--tag`, `--json` | Persist run results to experiment memory |
| `research memory list` | `--memory-file`, `--tag`, `--json` | Query experiment memory |

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
mypy coinjure/
pytest tests/ -v
```

## License

[MIT](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

## Disclaimer

This software is for educational and research use. Live trading carries financial risk.
