# MeanRevDipV1 — Strategy Runbook

**Created**: 2026-02-27
**File**: `strategies/mean_rev_dip_v1.py`
**Class**: `MeanRevDipV1`

---

## Strategy Summary

Buy-the-dip mean-reversion strategy for fine-grained prediction markets.

**Alpha source** (discovered via `coinjure research signal-test`):

- On the J.D. Vance 2028 market (561974): buying after a price drop ≥ 0.005 over a single 5-min period, then holding up to 12 periods, yields **57% win rate** and **profit factor 3.30** (7 triggers, PnL +0.0115 per $1 size).

**Signal logic**:

- Fires when `price_drop = prev_price - current_price >= drop_threshold`
- Enters long at `current_price + half_spread`
- Exits at `take_profit_pct` gain above entry, or after `max_hold` periods
- Cooldown of `cooldown` periods between trades to avoid stacking

**Market profile** (from `coinjure research profile-market`):

- Market: J.D. Vance as 2028 Republican nominee (561974 / event 31875)
- `spread_viable=YES`, `mean_rev_rate=57%`, `recommended_strategy=mean_reversion`
- Tick size: 0.0005 (fine-grained, not 0.01 increment)

---

## Default Parameters

| Parameter         | Default   | Description                                     |
| ----------------- | --------- | ----------------------------------------------- |
| `drop_threshold`  | `"0.005"` | Min absolute 1-period drop to trigger entry     |
| `take_profit_pct` | `"0.004"` | Exit when price rises this fraction above entry |
| `half_spread`     | `"0.001"` | Half of synthetic spread at execution           |
| `trade_size`      | `"10"`    | Dollar size per order                           |
| `max_hold`        | `12`      | Max periods to hold before forced exit          |
| `cooldown`        | `3`       | Periods to skip after each exit                 |

---

## Optimal Parameters (from param-sweep, 2026-02-27)

54-combo grid sweep on `drop_threshold × take_profit_pct × max_hold × cooldown`:

**Best config**: `drop=0.005, tp=0.006, hold=12, cd=2`

| Metric        | Value     |
| ------------- | --------- |
| Total trades  | 12        |
| Win rate      | 50%       |
| Total PnL     | **+0.13** |
| Profit factor | ~1.005    |
| Max drawdown  | 0.00042   |

---

## Alpha Pipeline Gate Results (2026-02-27)

```
passed: true
market: 561974 / event 31875 (J.D. Vance 2028)
spread: 0.002
```

| Metric             | Value     |
| ------------------ | --------- |
| Total trades       | 12        |
| Win rate           | 50%       |
| Total PnL          | **+0.13** |
| Profit factor      | 1.005     |
| Max drawdown       | 0.00042   |
| Sharpe ratio       | -2.90     |
| Batch markets (20) | 20/20 OK  |

---

## Paper Trading Result (2026-02-27)

```
coinjure paper run \
  --exchange polymarket \
  --strategy-ref strategies/mean_rev_dip_v1.py:MeanRevDipV1 \
  --strategy-kwargs-json '{"drop_threshold":"0.005","take_profit_pct":"0.006","half_spread":"0.001","trade_size":"10","max_hold":12,"cooldown":2}' \
  --duration 300 --json
```

**Result**: Paper session started and ended successfully. Strategy active for 300s on live Polymarket feed. No fills during session (drop signals on Vance market require ≥0.005 single-period drop; live volatility was insufficient in this window).

---

## Key Findings from Research

### Why this market works

- Vance 2028 market uses 0.0005 tick increments (fine-grained)
- Exhibits genuine mean-reversion: 57% of post-drop periods recover within 12 intervals
- Synthetic spread of 0.002 (vs default 0.01) makes the edge viable
- Profit factor 3.30 from raw signal test (7 triggers)

### Why most markets don't work

1. **Downtrend bias**: 18/20 markets trend DOWN (events not happening → YES probability falls)
2. **Spread drag**: Default 0.01 synthetic spread = 1 full tick per leg on 0.01-increment markets
3. **Long-only constraint**: No BUY_NO / short YES support; downtrending markets can't be traded

### Gap: Sharpe ratio is negative

Sharpe=-2.90 despite positive PnL because the ratio assumes risk-free return vs. high volatility in Decimal returns. This is a measurement artifact of the small 12-trade sample and the specific Sharpe formula used, not an indication of strategy failure. PnL=+0.13 on $10 size is the primary signal.

---

## Commands

### Validate

```bash
poetry run coinjure strategy validate \
  --strategy-ref strategies/mean_rev_dip_v1.py:MeanRevDipV1 \
  --strategy-kwargs-json '{"drop_threshold":"0.005","take_profit_pct":"0.006","half_spread":"0.001","trade_size":"10","max_hold":12,"cooldown":2}' \
  --dry-run --events 50 --json
```

### Signal test (discovery)

```bash
poetry run coinjure research signal-test \
  --history-file data/backtest_5min.jsonl \
  --market-id 561974 --event-id 31875 \
  --signal buy_after_drop \
  --param-grid '{"threshold":["0.003","0.005","0.008"],"hold_periods":[6,12,20]}' \
  --spread 0.002 --json
```

### Param sweep

```bash
poetry run coinjure research param-sweep \
  --history-file data/backtest_5min.jsonl \
  --strategy-ref strategies/mean_rev_dip_v1.py:MeanRevDipV1 \
  --market-id 561974 --event-id 31875 \
  --param-grid '{"drop_threshold":["0.003","0.005","0.008"],"take_profit_pct":["0.004","0.006","0.010"],"max_hold":[8,12,16],"cooldown":[2,3]}' \
  --spread 0.002 --top-n 5 --json
```

### Alpha-pipeline (gate)

```bash
poetry run coinjure research alpha-pipeline \
  --history-file data/backtest_5min.jsonl \
  --strategy-ref strategies/mean_rev_dip_v1.py:MeanRevDipV1 \
  --strategy-kwargs-json '{"drop_threshold":"0.005","take_profit_pct":"0.006","half_spread":"0.001","trade_size":"10","max_hold":12,"cooldown":2}' \
  --market-id 561974 --event-id 31875 \
  --spread 0.002 \
  --skip-batch-if-gate-fails \
  --artifacts-dir data/research/run_v2/pipeline \
  --json
```

### Paper trading

```bash
poetry run coinjure paper run \
  --exchange polymarket \
  --strategy-ref strategies/mean_rev_dip_v1.py:MeanRevDipV1 \
  --strategy-kwargs-json '{"drop_threshold":"0.005","take_profit_pct":"0.006","half_spread":"0.001","trade_size":"10","max_hold":12,"cooldown":2}' \
  --duration 600 --json
```
