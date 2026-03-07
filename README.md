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

The goal is simple: give an autonomous agent everything it needs to discover, test, and run strategies end-to-end — including managing a portfolio of ~50 parallel spread/arb strategies across Polymarket and Kalshi — while the human operator only does two things:

1. Monitor the system.
2. Pause or emergency-stop when needed.

## 1-Minute Start

```bash
coinjure engine run --exchange polymarket \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --mode paper --monitor
```

![Coinjure Monitor](assets/coinjure_monitor.png)

## Why Agent-First

AI agents are now strong enough to self-discover alpha from messy, fast-moving market and news streams. The practical question is no longer "can an agent reason about trades," but:

Can we build the tools, infrastructure, and environment that let an agent operate like a disciplined human trader, but through command-line APIs that are easy for agents to use?

Coinjure is our answer:

- Strategy is the primary interface.
- The engine owns execution, risk checks, and lifecycle.
- Data, simulation, monitoring, and live controls are unified behind CLI commands.

This lets agents iterate quickly, safely, and reproducibly without rebuilding trading plumbing for every strategy or exchange.

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
- `StrategyRegistry`: persistent portfolio registry (`~/.coinjure/portfolio.json`).
- `RelationStore`: persistent market relation graph (`~/.coinjure/relations.json`).

```text
Agent (Claude)
     | bash calls (--json)
     v
CLI Layer (4 groups)
  coinjure market    — discover, analyze, relations, news
  coinjure strategy  — validate, backtest, batch, pipeline, gate
  coinjure engine    — run, list, add, deploy, supervise, allocate, pipeline, hub-*
  coinjure memory    — add, list, best, summary
     | reads/writes ~/.coinjure/
     | spawns subprocesses
     v
[strategy-001]   [strategy-002]  ...  [strategy-050]
engine run       engine run            engine run
(paper process)  (paper process)       (live process)
sock: 001.sock   sock: 002.sock        sock: 050.sock

Each strategy process:
TradingEngine
  |- DataSource (market/news/backtest)
  |- Strategy (agent logic)
  |- Trader (paper/live exchange adapter)
  |  |- RiskManager
  |  |- PositionManager
  |- ControlServer (per-strategy socket)
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

## Quick Start

### 1) Explore markets

```bash
coinjure market discover -q "election" --exchange both --limit 20 --json
coinjure market analyze --exchange polymarket --market-id <market_id> --json
coinjure market news --source google --query "prediction market" --limit 10
```

### 2) Validate a strategy

```bash
coinjure strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Dry-run smoke check before backtest
coinjure strategy validate \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --dry-run --events 10 --json
```

### 3) Backtest

```bash
coinjure strategy backtest \
  --history-file ./data/events.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy --json
```

### 4) Paper trading

```bash
coinjure engine run \
  --exchange polymarket --mode paper \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"trade_size": "25"}' \
  --monitor
```

### 5) Runtime control (separate terminal)

```bash
coinjure engine status --id my-strategy-001
coinjure engine pause  --id my-strategy-001
coinjure engine resume --id my-strategy-001
coinjure engine stop   --id my-strategy-001
```

## Spread Trading System

Coinjure includes a spread trading system that discovers, validates, and manages cross-market spread/arbitrage opportunities:

1. **Market Discovery** (`market discover`) — multi-keyword search + structural pair detection (temporal implication, cross-platform match, complementary outcomes).
2. **Quantitative Analysis** (`market analyze`) — single-market stats or pair analysis (correlation, cointegration, hedge ratio, half-life).
3. **Relation Management** (`market relations`) — persistent graph of discovered pairs with lifecycle (active → validated → deployed → retired).
4. **Live Maintenance** (`engine supervise`) — continuously validates running strategies.

### Discover spread opportunities

```bash
# Search markets and find structural spread pairs
coinjure market discover -q "election" -q "Trump" --exchange both --limit 50 --json
# -> persists discovered pairs to ~/.coinjure/relations.json

# Quantitative analysis of a single market
coinjure market analyze --exchange polymarket --market-id <id> --json

# Pair analysis (correlation, cointegration, spread stats)
coinjure market analyze --exchange polymarket --market-id <id_a> --compare <id_b> --json
```

### Manage discovered relations

```bash
coinjure market relations list --type same_event --json
coinjure market relations add --market-id-a <id_a> --market-id-b <id_b> --spread-type implication --json
coinjure market relations remove <relation_id>
```

### LLM supervision and capital allocation

```bash
# LLM reviews all active strategies, recommends hold/pause/retire
coinjure engine supervise --json
coinjure engine supervise --execute  # auto-apply recommendations

# Deep validity analysis of a single strategy
coinjure engine supervise --id my-strategy-001 --json

# Capital allocation across strategies
coinjure engine allocate --method kelly --max-exposure 10000 --json
```

## Portfolio Management

Manage ~50 parallel strategy instances through a single registry:

```bash
# Register a new strategy
coinjure engine add \
  --strategy-id arb-nba-001 \
  --strategy-ref examples/strategies/direct_arb_strategy.py:DirectArbStrategy \
  --kwargs-json '{"poly_market_id": "xxx", "kalshi_ticker": "NBANBA-GSW"}' --json

# Deploy to paper trading
coinjure engine deploy --strategy-id arb-nba-001 --json

# Portfolio report with health diagnostics
coinjure engine report --check-health --json

# Retire a stale strategy
coinjure engine retire --id arb-nba-001 --reason "market_closed" --json

# View all strategies
coinjure engine list --json

# Bulk operations
coinjure engine pause --all --json
coinjure engine stop --all --json
coinjure engine retire --all --reason "end_of_season" --json
```

## Human-in-the-Loop Model

The operator is intentionally lightweight:

- Use `coinjure engine monitor` for live visibility.
- Use `coinjure engine pause|resume|stop` for intervention.
- Use `coinjure engine killswitch --on` for emergency halt.

The operator should not need to manually place/cancel orders in normal operation.

## CLI Reference

### `coinjure market` — Market discovery and analysis

| Command                      | Description                                                        |
| ---------------------------- | ------------------------------------------------------------------ |
| `market analyze`             | Quantitative analysis of a market or pair of markets               |
| `market discover`            | Multi-keyword search + structural spread pair discovery            |
| `market news`                | Fetch news headlines                                               |
| `market relations list`      | List stored market relations                                       |
| `market relations add`       | Create a relation between two markets                              |
| `market relations remove`    | Remove a relation                                                  |

### `coinjure strategy` — Strategy development and testing

| Command             | Description                                  |
| ------------------- | -------------------------------------------- |
| `strategy validate` | Import-check and optionally feed mock events |
| `strategy backtest` | Run a backtest against a local history file  |

### `coinjure engine` — Running instances and portfolio management

| Command             | Description                                                        |
| ------------------- | ------------------------------------------------------------------ |
| `engine run`        | Start trading (`--mode paper\|live`)                               |
| `engine list`       | Show all strategies with lifecycle and PnL                         |
| `engine add`        | Register a new strategy                                            |
| `engine status`     | Show engine status (`--full` for positions, PnL, order books)      |
| `engine pause`      | Pause event ingestion (`--all` for all instances)                  |
| `engine resume`     | Resume after pause                                                 |
| `engine stop`       | Graceful shutdown (`--all` for all instances)                      |
| `engine swap`       | Hot-swap strategy without restarting                               |
| `engine retire`     | Stop and mark as retired (`--all` for all instances)               |
| `engine deploy`     | Scan and batch-deploy strategies (`--mode cross-platform\|events`) |
| `engine report`     | Portfolio PnL report (`--check-health` for diagnostics)            |
| `engine feedback`   | Record feedback on a strategy                                      |
| `engine monitor`    | Attach Textual TUI to a running engine                             |
| `engine killswitch` | Toggle the global kill-switch                                      |
| `engine supervise`  | LLM review (`--id` for single, omit for all strategies)            |
| `engine allocate`   | Capital allocation across strategies                               |
### `coinjure hub` — Shared Market Data Hub

| Command      | Description                                |
| ------------ | ------------------------------------------ |
| `hub start`  | Start the shared Market Data Hub           |
| `hub status` | Show hub status                            |
| `hub stop`   | Stop the hub                               |

### `coinjure memory` — Experiment memory

| Command          | Description                              |
| ---------------- | ---------------------------------------- |
| `memory add`     | Persist run results to experiment memory |
| `memory list`    | Query experiment memory                  |
| `memory best`    | Show best runs                           |
| `memory summary` | Summarize experiment history             |

### `coinjure research` — Research tooling

| Command                    | Description                         |
| -------------------------- | ----------------------------------- |
| `research strategy-gate`   | Promotion gate with thresholds      |
| `research alpha-pipeline`  | Full alpha discovery pipeline       |
| `research batch-markets`   | Run strategy across N markets       |
| `research harvest`         | Harvest results from completed runs |
| `research feedback-report` | Generate feedback report            |
| `research market-snapshot` | Snapshot market state               |

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
