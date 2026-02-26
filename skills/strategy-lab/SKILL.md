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
# Single market test (fast, good for debugging)
coinjure backtest run \
  --history-file data/backtest_data.jsonl \
  --market-id 516926 --event-id 16167 \
  --strategy-ref strategies/my_strategy.py:MyStrategy

# Multi-market batch test (create a subset, loop through)
head -20 data/backtest_data.jsonl > /tmp/bt_20.jsonl

python3 -c "
import json, subprocess, sys
results = []
with open('/tmp/bt_20.jsonl') as f:
    markets = [json.loads(line) for line in f]
for i, d in enumerate(markets):
    mid, eid = d['market_id'], d['event_id']
    print(f'\\n--- [{i+1}/{len(markets)}] market={mid} ---')
    subprocess.run([
        'coinjure', 'backtest', 'run',
        '--history-file', '/tmp/bt_20.jsonl',
        '--market-id', mid, '--event-id', eid,
        '--strategy-ref', 'strategies/my_strategy.py:MyStrategy',
    ])
"
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

# Backtest (need market_id + event_id from the JSONL file)
coinjure backtest run \
  --history-file data/backtest_data.jsonl \
  --market-id MID --event-id EID \
  --strategy-ref strategies/X.py:X

# Paper trading (live data, simulated execution)
coinjure paper run --exchange polymarket --strategy-ref strategies/X.py:X
coinjure paper run --exchange kalshi --strategy-ref strategies/X.py:X

# Live trading (real money)
coinjure live run --exchange kalshi --strategy-ref strategies/X.py:X
```
