![Coinjure](assets/coinjure.png)

<h1 align="center">
  Agent-Native Trading System for Prediction Markets
</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/coinjure/"><img src="https://img.shields.io/pypi/v/coinjure.svg" alt="PyPI version"></a>
  <a href="https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

**Coinjure** is a full-stack, CLI-native trading framework designed for LLM agents in prediction markets. It empowers agents to drive the entire strategy lifecycle purely by interacting with a command-line interface. By simply issuing CLI commands, an agent can autonomously discover cross-market relations, compile executable strategies, run large-scale backtests, and deploy to live execution.

Using Coinjure, agents like Claude Code or Codex can discover over 100 backtest-positive strategies in a single hour — a capability validated by deploying to live trading and generating real profit on prediction market exchanges.

## Why Agent-Native

Trading is entering an AI-native era. Prediction markets are fundamentally semantic and event-driven — success depends on interpreting political shifts, breaking news, and evolving public narratives. These are domains where LLM agents uniquely excel.

Most trading platforms today are built around visual dashboards. For an AI agent, however, a CLI is the natural environment. Coinjure is architected for agent-first access:

- **Strategy is the primary interface.** Every exploitable opportunity stems from a _relation_ between markets, and every relation type maps 1-to-1 to a _strategy_ that trades it.
- **The engine owns execution.** Risk checks, lifecycle management, and process isolation are handled automatically.
- **Data, simulation, monitoring, and live controls are unified behind CLI commands** — letting agents iterate quickly, safely, and reproducibly.

## System Overview

The pipeline has four stages:

```
LLM Agent + market data
    → [1. Discovery]  — discover cross-market relations via CLI
    → [2. Backtest]   — validate each relation against historical data
    → [3. Execution]  — paper-trade or live-trade validated strategies
    → [4. Monitoring]  — human operator monitors and intervenes when needed
```

Core runtime components:

```
TradingEngine
  ├── DataSource          (Live / Historical / Hub)
  ├── Strategy            (Relation-based / LLM-powered / Custom)
  └── Trader              (PaperTrader / PolymarketTrader / KalshiTrader)
       ├── RiskManager    (Standard / Conservative / Aggressive)
       └── PositionManager
```

Each engine instance runs as an independent OS process, allowing hundreds of strategies to execute in parallel without shared-state contention. A `ControlServer` (Unix socket) provides per-instance pause/resume/status/stop, while the `StrategyRegistry` persists portfolio state across sessions.

## Relation-Strategy Mapping

The LLM agent reads free-text market descriptions to discover relations; Coinjure automatically instantiates the corresponding strategy. Discovering a new relation immediately yields a ready-to-backtest strategy with no additional code.

| Relation          | Constraint                        | Strategy                 |
| ----------------- | --------------------------------- | ------------------------ |
| **same_event**    | Identical market across platforms | `DirectArbStrategy`      |
| **complementary** | Outcomes sum to 1                 | `GroupArbStrategy`       |
| **implication**   | A ⇒ B price ordering              | `ImplicationArbStrategy` |
| **exclusivity**   | Mutually exclusive                | `GroupArbStrategy`       |
| **correlated**    | Cointegrated prices               | `CointSpreadStrategy`    |
| **structural**    | Monotonic price nesting           | `StructuralArbStrategy`  |
| **conditional**   | Conditional probability bounds    | `ConditionalArbStrategy` |
| **temporal**      | Lead-lag information flow         | `LeadLagStrategy`        |

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

### 1) Discover — find markets and detect tradable relations

```bash
coinjure market discover -q "election" --exchange both --limit 20
```

Searches Polymarket and Kalshi, then auto-detects structural relations (exclusivity, complementary, implication) within each event. Discovered relations are persisted to `~/.coinjure/relations.json`.

### 2) Backtest — validate strategies against historical data

```bash
coinjure engine backtest --all-relations
```

Each relation type auto-selects its matching strategy (see table above). Only relations with positive PnL advance to the next stage.

### 3) Paper trade — simulate live execution

```bash
coinjure engine paper-run --all-relations --detach
```

Deploys all backtest-passed strategies as independent background processes. Use `coinjure engine monitor` to attach a live TUI dashboard.

### 4) Live trade — execute with real funds

```bash
coinjure engine live-run --exchange polymarket --all-relations --detach
```

Requires exchange credentials (see [Environment Variables](#environment-variables)).

### 5) Monitor and control

```bash
coinjure engine monitor         # live TUI across all engines
coinjure engine pause  --all    # pause all strategies
coinjure engine resume --all    # resume
coinjure engine killswitch --on # emergency halt
```

![Coinjure Monitor](assets/coinjure_monitor.png)

## Custom Strategy

```python
from coinjure.strategy.strategy import Strategy
from coinjure.events import Event
from coinjure.trading.trader import Trader

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your logic here
        pass
```

## Market Data Hub

For running many strategies against the same markets, the hub polls exchanges once and fans out data to all subscribers via Unix sockets:

```bash
coinjure hub start --detach
coinjure hub status
coinjure hub stop
```

Engine instances auto-connect to the hub when it's running (disable with `--no-hub`).

## Human-in-the-Loop Model

The operator is intentionally lightweight:

- Use `coinjure engine monitor` for live TUI visibility across all running engines.
- Use `coinjure engine pause|resume|stop` for intervention.
- Use `coinjure engine killswitch --on` for emergency halt.
- Use `coinjure engine swap` for hot-swapping strategy logic without restarting.

The operator should not need to manually place/cancel orders in normal operation.

## CLI Reference

### `coinjure market` — Market discovery and analysis

| Command                   | Description                                               |
| ------------------------- | --------------------------------------------------------- |
| `market info`             | Fetch detailed info for a market (by ID or slug)          |
| `market discover`         | Multi-keyword search + auto structural relation discovery |
| `market news`             | Fetch news headlines (Google, RSS, TheNewsAPI)            |
| `market relations list`   | List stored market relations (filter by type/status)      |
| `market relations add`    | Create a relation between 2+ markets                      |
| `market relations remove` | Remove a relation by ID                                   |

### `coinjure engine` — Trading engine and portfolio management

| Command             | Description                                                      |
| ------------------- | ---------------------------------------------------------------- |
| `engine paper-run`  | Start paper trading (single strategy or `--all-relations` batch) |
| `engine live-run`   | Start live trading with real funds                               |
| `engine backtest`   | Backtest relations against historical order book data            |
| `engine list`       | Show all strategies in the portfolio registry                    |
| `engine status`     | Show engine status: positions, PnL, decisions, trades            |
| `engine pause`      | Pause decision-making (`--all` for all instances)                |
| `engine resume`     | Resume after pause                                               |
| `engine stop`       | Graceful shutdown (`--all` for all instances)                    |
| `engine swap`       | Hot-swap strategy without restarting                             |
| `engine retire`     | Stop and mark as retired (`--all` for all instances)             |
| `engine promote`    | Promote relation(s) from paper to deployed                       |
| `engine monitor`    | Attach live TUI dashboard to running engines                     |
| `engine killswitch` | Toggle the global emergency kill-switch                          |

### `coinjure hub` — Shared Market Data Hub

| Command      | Description                            |
| ------------ | -------------------------------------- |
| `hub start`  | Start the exchange data fan-out server |
| `hub status` | Show hub status and subscriber count   |
| `hub stop`   | Stop the hub                           |

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"      # Polymarket live trading
export KALSHI_API_KEY_ID="your_kalshi_key_id"         # Kalshi live trading
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"        # Kalshi live trading
```

## Development

```bash
poetry install --with dev,test
pre-commit install
ruff check . && ruff format .
pytest tests/ -v -p no:nbmake
```

## Citation

If you find Coinjure useful in your research or work, please cite:

```bibtex
@software{coinjure2026,
  title   = {Coinjure: An Agent-Native Trading System for Prediction Markets},
  author  = {Yu, Haofei and Yang, Yicheng and Liu, Yuxiang and You, Jiaxuan},
  year    = {2026},
  url     = {https://github.com/ulab-uiuc/prediction-market-cli},
  note    = {University of Illinois Urbana-Champaign}
}
```

## License

[MIT](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

## Disclaimer

This software is for educational and research use. Live trading carries financial risk.
