---
name: strategy-lab
description: Autonomous prediction market strategy research lab. Use when asked to develop, test, iterate, or improve prediction market trading strategies. Covers the full lifecycle from idea → code → backtest → analysis → iteration.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, WebSearch, WebFetch
---

# Strategy Lab — Autonomous Prediction Market Strategy Development

You are a quantitative researcher with a fully-equipped prediction market trading workstation. Your goal is to autonomously develop profitable strategies through systematic research, coding, backtesting, and iteration.

## Your Workstation

```
coinjure/strategy/                  # Strategy code (reference implementations)
│   ├── strategy.py               # Base class (inherit from this)
│   ├── simple_strategy.py        # Reference: LLM-based strategy
│   ├── test_strategy.py          # Reference: simple momentum strategy
│   ├── market_making_strategy.py # Reference: market making
│   └── orderbook_imbalance_strategy.py  # Reference: OBI strategy
data/
│   └── backtest_data.jsonl       # 8769 prediction markets (7.4MB, ready to use)
scripts/
│   └── convert_hf_dataset.py     # HuggingFace data converter (already run)
strategies/                       # YOUR workspace for custom strategies
```

## Quick Reference: Strategy API

### Base Class

```python
from coinjure.strategy.strategy import Strategy
from coinjure.events.events import Event, NewsEvent, PriceChangeEvent, OrderBookEvent
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide
from decimal import Decimal

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Called for EVERY event. Decide what to do.
        pass
```

### Events You Receive

```python
# PriceChangeEvent — market probability changed
event.ticker        # PolyMarketTicker with .symbol, .name, .market_id, .event_id
event.price         # Decimal: implied probability (0.0 to 1.0)
event.timestamp     # string (ISO 8601) in backtest, datetime in live

# NewsEvent — relevant news (live mode only: from RSS/API)
event.title         # headline string
event.news          # full article text
event.source        # e.g. "Reuters", "CNN"
event.ticker        # linked market ticker (may be None)

# OrderBookEvent — bid/ask depth changed (live mode only)
event.ticker, event.price, event.size, event.side  # 'bid' or 'ask'
```

**Important**: In backtest mode with `data/backtest_data.jsonl`, your strategy only receives `PriceChangeEvent`. NewsEvent and OrderBookEvent are live-mode only.

### Trader API

```python
# Place orders
result = await trader.place_order(
    side=TradeSide.BUY,      # or TradeSide.SELL
    ticker=event.ticker,
    limit_price=Decimal('0.55'),
    quantity=Decimal('100'),
)
# result.order.status: FILLED, PARTIALLY_FILLED, REJECTED
# result.order.average_price, result.order.filled_quantity

# Read market data
bid = trader.market_data.get_best_bid(ticker)   # Level(price, size) or None
ask = trader.market_data.get_best_ask(ticker)   # Level(price, size) or None

# Read positions
pos = trader.position_manager.get_position(ticker)
# pos.quantity, pos.average_cost, pos.realized_pnl

cash = trader.position_manager.get_cash_positions()  # [Position] with .quantity
all_pos = trader.position_manager.get_non_cash_positions()
```

## Backtest Data

`data/backtest_data.jsonl` — 8769 Polymarket prediction markets, each line:

```json
{"event_id": "16167", "market_id": "516926", "time_series": {"Yes": [{"t": "2025-12-06T06:00:14+00:00", "p": 0.0225}, ...]}}
```

Source: HuggingFace `lwaekfjlk/prediction-market-news` (already converted).
One backtest run = one `--market-id` + `--event-id` pair from this file.

## Development Workflow

### Step 1: Explore the data

```bash
# List available markets with their IDs
python3 -c "
import json
with open('data/backtest_data.jsonl') as f:
    for i, line in enumerate(f):
        if i >= 20: break
        d = json.loads(line)
        pts = len(d['time_series']['Yes'])
        print(f'market_id={d[\"market_id\"]:>10}  event_id={d[\"event_id\"]:>10}  pts={pts}')
"

# Analyze price movement patterns across all markets
python3 -c "
import json
changes, sizes = [], []
with open('data/backtest_data.jsonl') as f:
    for line in f:
        d = json.loads(line)
        pts = d['time_series']['Yes']
        sizes.append(len(pts))
        if len(pts) >= 2:
            changes.append(pts[-1]['p'] - pts[0]['p'])
n = len(changes)
print(f'Total markets: {len(sizes)}')
print(f'Avg price points/market: {sum(sizes)/len(sizes):.1f}')
print(f'Mean price change: {sum(changes)/n:.4f}')
print(f'Moved up >5%: {sum(1 for c in changes if c > 0.05)} ({sum(1 for c in changes if c > 0.05)/n*100:.1f}%)')
print(f'Moved down >5%: {sum(1 for c in changes if c < -0.05)} ({sum(1 for c in changes if c < -0.05)/n*100:.1f}%)')
print(f'Stayed flat: {sum(1 for c in changes if abs(c) < 0.02)} ({sum(1 for c in changes if abs(c) < 0.02)/n*100:.1f}%)')
"
```

### Step 2: Write your strategy

```bash
mkdir -p strategies

# Use the template generator
coinjure strategy create --output strategies/my_strategy.py --class-name MyStrategy

# Or write directly
```

Key rules:

- Must inherit from `Strategy`
- Must implement `async def process_event(self, event, trader)`
- Use `Decimal` for all prices/quantities (never `float`)

### Step 3: Validate

```bash
coinjure strategy validate \
  --strategy-ref strategies/my_strategy.py:MyStrategy --json
```

### Step 4: Backtest

```bash
# Single market — JSON output (agent-parseable, no interactive noise)
coinjure backtest run \
  --history-file data/backtest_data.jsonl \
  --market-id 516926 --event-id 16167 \
  --strategy-ref strategies/my_strategy.py:MyStrategy \
  --json
# → {"ok": true, "total_trades": 12, "win_rate": "0.583", "sharpe_ratio": "1.24", ...}

# Grid search best params on that market
coinjure research grid \
  --history-file data/backtest_data.jsonl \
  --market-id 516926 --event-id 16167 \
  --strategy-ref strategies/my_strategy.py:MyStrategy \
  --param-grid-json '{"threshold": [0.005, 0.01, 0.02], "trade_size": [25, 50, 100]}' \
  --output /tmp/grid.jsonl --json
# → {"runs": 9, "best": {"threshold": 0.01, "trade_size": 50, "sharpe_ratio": "1.31"}}

# Multi-market sweep — validate it generalises (no shell loop needed)
coinjure research batch-markets \
  --history-file data/backtest_data.jsonl \
  --strategy-ref strategies/my_strategy.py:MyStrategy \
  --strategy-kwargs-json '{"threshold": 0.01, "trade_size": 50}' \
  --limit 50 \
  --output /tmp/batch.jsonl --json
# → {"ok_markets": 42, "aggregate": {"mean_sharpe": "0.82", "pct_profitable": "68.0", ...}}
```

### Step 5: Analyze & iterate

After backtest, look at the PERFORMANCE SUMMARY:

| Metric       | Good Sign | Bad Sign | Fix                               |
| ------------ | --------- | -------- | --------------------------------- |
| Win Rate     | >55%      | <45%     | Adjust entry/exit thresholds      |
| Total Return | >0%       | <0%      | Signal logic may be wrong         |
| Sharpe       | >0.5      | <0       | Too noisy; reduce trade frequency |
| Max Drawdown | <10%      | >20%     | Add position limits / stop-losses |
| Total Trades | >5        | 0-1      | Lower thresholds to trade more    |

Iterate: adjust parameters → re-backtest → compare results.

## Strategy Ideas

### 1. Mean Reversion

Prediction markets overreact to news. Buy when price drops significantly below recent average, sell on recovery.

### 2. Momentum / Trend Following

Some markets trend persistently. Buy into rising probabilities, sell into falling ones.

### 3. Volatility-Based Sizing

Trade larger when volatility is low, smaller when high. Track rolling std dev of price changes.

### 4. Contrarian

Buy when large drops happen (capitulation). Works well near extreme probabilities (< 0.1 or > 0.9).

### 5. Calendar Effect

Markets behave differently near resolution. Probabilities compress toward 0 or 1. Trade the compression.

### 6. Statistical Arbitrage

Compare price levels across related markets. If correlated markets diverge, trade the convergence.

## Reference: SimpleStrategy Patterns

Read `coinjure/strategy/simple_strategy.py` for production-grade patterns:

- **Edge calculation**: `edge = abs(llm_prob - market_price)`, trade only if edge > 10%
- **Position exits**: timeout (1hr), edge consumed (<3% remaining), edge reversed
- **LLM re-evaluation**: check position thesis every 5 min
- **News buffering**: keyword matching for relevance
- **Dual-side trading**: BUY YES when overvalued, BUY NO when undervalued

## Commands Cheat Sheet

```bash
# Strategy development
coinjure strategy create --output strategies/X.py --class-name X
coinjure strategy validate --strategy-ref strategies/X.py:X --json

# Backtest — JSON output (agent-friendly, silent)
coinjure backtest run \
  --history-file data/backtest_data.jsonl \
  --market-id MID --event-id EID \
  --strategy-ref strategies/X.py:X \
  --json
# → {"ok": true, "total_trades": 12, "win_rate": "0.583", "sharpe_ratio": "1.24", ...}

# Multi-market sweep — test strategy across N markets, get aggregate stats
coinjure research batch-markets \
  --history-file data/backtest_data.jsonl \
  --strategy-ref strategies/X.py:X \
  --limit 50 \
  --output /tmp/batch.jsonl \
  --json
# → {"ok_markets": 42, "total_markets": 50, "aggregate": {"mean_sharpe": "0.82", ...}}

# Hyperparameter grid search on one market
coinjure research grid \
  --history-file data/backtest_data.jsonl \
  --market-id MID --event-id EID \
  --strategy-ref strategies/X.py:X \
  --param-grid-json '{"threshold": [0.01, 0.02, 0.05], "size": [25, 50, 100]}' \
  --output /tmp/grid.jsonl \
  --json
# → {"runs": 9, "ok_runs": 9, "best": {"threshold": 0.02, "size": 50, "sharpe_ratio": "1.31"}}

# Live price history (no local file needed)
coinjure market history --market-id MID --interval 1d --limit 30 --json
# → {"points": 30, "series": [...], "first_price": 0.42, "last_price": 0.71, "total_move": 0.29}

# Paper trading (live data, simulated execution)
coinjure paper run --exchange polymarket --strategy-ref strategies/X.py:X
coinjure paper run --exchange kalshi --strategy-ref strategies/X.py:X

# Live trading (real money)
coinjure live run --exchange kalshi --strategy-ref strategies/X.py:X
```

---

## Live Engine Mode (when engine is running in right pane)

Use these commands when a paper/live engine is already running (started via `./scripts/start-lab.sh` or manually). The engine keeps streaming market data and managing positions continuously; your job in the left pane is to research, write, and hot-swap better strategies.

### Check engine health

```bash
coinjure trade status --json
```

Returns: `ok`, `paused`, `runtime`, `event_count`, `decisions`, `executed`, `orders`.

### Get full snapshot (positions, PnL, order books, decisions)

```bash
coinjure trade get-state --json
```

Returns a JSON object with keys:

| Key | Contents |
|-----|----------|
| `positions` | All open non-cash positions with qty, avg cost, bid, PnL |
| `portfolio` | Total value, cash positions, realized/unrealized PnL |
| `decisions` | Last 40 strategy decisions with action, confidence, reasoning |
| `order_books` | Top 40 active markets sorted by proximity to 50% |
| `orders` | Last 8 orders with side, price, status |
| `stats` | Event count, order book count, decision stats |
| `activity_log` | Recent engine activity entries |
| `news` | Buffered news items |

### Hot-swap strategy into running engine

```bash
coinjure trade swap-strategy \
  --strategy-ref strategies/my_strategy.py:MyStrategy \
  [--strategy-kwargs-json '{"param": "val"}'] \
  --json
```

The engine **pauses**, loads the new class, instantiates it, replaces the active strategy, then **resumes**. Existing positions are preserved — the new strategy takes over managing them.

### Autonomous Research-and-Swap Loop

When invoked in the left pane while a live engine runs in the right pane:

1. **Scan** — quick price check: is this market worth backtesting?
   ```bash
   coinjure market history --market-id MID --interval 1d --limit 30 --json
   # → total_move: 0.29 — active market, good candidate
   ```

2. **Write** — save new strategy hypothesis
   ```bash
   coinjure strategy create --output strategies/hypothesis_N.py --class-name HypothesisN
   # Edit the file with your signal logic
   ```

3. **Validate** — confirm the strategy loads and constructs
   ```bash
   coinjure strategy validate --strategy-ref strategies/hypothesis_N.py:HypothesisN --json
   ```

4. **Grid search** — find best params on one market
   ```bash
   coinjure research grid \
     --history-file data/backtest_data.jsonl \
     --market-id MID --event-id EID \
     --strategy-ref strategies/hypothesis_N.py:HypothesisN \
     --param-grid-json '{"threshold": [0.005, 0.01, 0.02], "trade_size": [25, 50, 100]}' \
     --output /tmp/grid.jsonl --json
   # → best: {threshold: 0.01, trade_size: 50, sharpe: 1.31}
   ```

5. **Generalise** — sweep across 50 markets to confirm it's not overfit
   ```bash
   coinjure research batch-markets \
     --history-file data/backtest_data.jsonl \
     --strategy-ref strategies/hypothesis_N.py:HypothesisN \
     --strategy-kwargs-json '{"threshold": 0.01, "trade_size": 50}' \
     --limit 50 --output /tmp/batch.jsonl --json
   # → mean_sharpe: 0.72, pct_profitable: 64%  ✓ good signal
   ```

6. **Gate check** — enforce minimum quality bars
   ```bash
   coinjure research strategy-gate \
     --history-file data/backtest_data.jsonl \
     --market-id MID --event-id EID \
     --strategy-ref strategies/hypothesis_N.py:HypothesisN \
     --strategy-kwargs-json '{"threshold": 0.01, "trade_size": 50}' \
     --json
   ```

7. **Swap** — hot-swap into the running engine
   ```bash
   coinjure trade swap-strategy \
     --strategy-ref strategies/hypothesis_N.py:HypothesisN \
     --strategy-kwargs-json '{"threshold": 0.01, "trade_size": 50}' \
     --json
   ```

8. **Monitor** — poll status every 60 s for 5–10 min
   ```bash
   for i in $(seq 1 10); do
     coinjure trade status --json
     sleep 60
   done
   ```

9. **Evaluate** — if live performance is poor (decisions executed = 0, PnL trending down), go back to step 1 with a new hypothesis. If performance looks good, continue monitoring.

### Start the two-pane lab session

```bash
./scripts/start-lab.sh [exchange] [initial-strategy-ref]
# Example:
./scripts/start-lab.sh polymarket coinjure.strategy.simple_strategy:SimpleStrategy
```

This launches a tmux session with:
- **Right pane (60%)** — paper trading engine with Textual TUI monitor
- **Left pane (40%)** — Claude Code agent for autonomous strategy research
