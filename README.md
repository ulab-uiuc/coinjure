![Coinjure](assets/coinjure.png)

<h1 align="center">
  Trading Agent Harness for Prediction Markets
</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/coinjure/"><img src="https://img.shields.io/pypi/v/coinjure.svg" alt="PyPI version"></a>
  <a href="https://github.com/ulab-uiuc/coinjure/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
  <a href="https://join.slack.com/t/coinjure/shared_invite/zt-3uji79vht-thjQnb4LJtLrN13pifTIuw"><img src="https://img.shields.io/badge/Slack-Join%20Us-purple?logo=slack" alt="Slack"></a>
  <a href="assets/coinjure_wechat_link.jpg"><img src="https://img.shields.io/badge/WeChat-Join%20Us-green?logo=wechat" alt="WeChat"></a>
  <a href="https://ulab-uiuc.github.io/coinjure/"><img src="https://img.shields.io/badge/Blog-Read-orange" alt="Blog"></a>
  <a href="https://ulab-uiuc.github.io/coinjure/docs/index.html"><img src="https://img.shields.io/badge/Docs-Read-blue" alt="Docs"></a>
</p>

**Coinjure** is a trading agent harness for prediction markets. It empowers LLM agents to drive the entire strategy lifecycle purely through CLI commands — autonomously discovering cross-market relations, building executable strategies, running large-scale backtests, and deploying to live execution.

Using Coinjure, LLM agents can discover over 100 backtest-positive strategies in a single hour — a capability validated by deploying to live trading and generating real profit on prediction market exchanges.

## Demos

- [1min Introduction of Coinjure](https://youtu.be/TbHq0bI6hvo?si=PPoIEvU4bLQP1j-W)
- [Basic Functionality Demo of Coinjure](https://youtu.be/1Iro-NPfFnY)
- [Claude Code + Coinjure Demo](https://youtu.be/2gxla8qlrDU)

## Installation

```bash
pip install coinjure
```

From source:

```bash
git clone https://github.com/ulab-uiuc/coinjure.git
cd coinjure
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

## Architecture Overview

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

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"      # Polymarket live trading
export KALSHI_API_KEY_ID="your_kalshi_key_id"         # Kalshi live trading
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"        # Kalshi live trading
```

## Citation

If you find Coinjure useful in your research or work, please cite:

```bibtex
@software{coinjure2026,
  title   = {Coinjure: A Trading Agent Harness for Prediction Markets},
  author  = {Yu, Haofei and Yang, Yicheng and Liu, Yuxiang and You, Jiaxuan},
  year    = {2026},
  url     = {https://github.com/ulab-uiuc/coinjure},
  note    = {University of Illinois Urbana-Champaign}
}
```

## License

[MIT](https://github.com/ulab-uiuc/coinjure/blob/main/LICENSE)

## Disclaimer

This software is for educational and research use. Live trading carries financial risk.
