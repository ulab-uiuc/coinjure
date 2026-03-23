# Engine

Module: `coinjure.engine.engine`

The `TradingEngine` is the core event loop that connects data sources, strategies, and traders.

## TradingEngine

```python
from decimal import Decimal
from coinjure.engine.engine import TradingEngine

engine = TradingEngine(
    data_source=source,
    strategy=strategy,
    trader=trader,
    initial_capital=Decimal("10000"),
    continuous=True,          # keep running when no events (live mode)
    state_store=state_store,  # optional: persist positions/trades
    alerter=alerter,          # optional: trade/risk notifications
    drawdown_alert_pct=Decimal("0.15"),  # optional: alert threshold
)

await engine.start()
```

### Constructor Parameters

| Parameter            | Type                 | Default | Description                                              |
| -------------------- | -------------------- | ------- | -------------------------------------------------------- |
| `data_source`        | `DataSource`         | —       | Event stream (live or historical)                        |
| `strategy`           | `Strategy`           | —       | Trading strategy instance                                |
| `trader`             | `Trader`             | —       | Order execution (paper or live)                          |
| `initial_capital`    | `Decimal`            | `10000` | Starting capital for performance tracking                |
| `continuous`         | `bool`               | `False` | `True` for live trading (keeps looping on `None` events) |
| `state_store`        | `StateStore \| None` | `None`  | JSON persistence for positions/trades                    |
| `alerter`            | `Alerter \| None`    | `None`  | Notification backend                                     |
| `drawdown_alert_pct` | `Decimal \| None`    | `None`  | Drawdown percentage to trigger alert                     |

### Event Loop

The engine processes events in a loop:

1. **Fetch** — `data_source.get_next_event()`
2. **Batch** — In backtest mode, drain all same-timestamp events. In live mode, drain all pending.
3. **Update** — Feed events to `DataManager` (order books, prices)
4. **Fill** — Attempt to fill resting orders against updated books
5. **Evaluate** — Call `strategy.process_event(event, trader)`
6. **Sync** — Record trades, persist state, send alerts

### Periodic Checks

| Interval         | Action                                                      |
| ---------------- | ----------------------------------------------------------- |
| Every 100 events | Drawdown alert, portfolio health gate, state persistence    |
| Every 500 events | LLM portfolio review (if enabled), stale order book pruning |

### Safety Features

| Feature                   | Behavior                                                          |
| ------------------------- | ----------------------------------------------------------------- |
| **Auto-degrade**          | After 5 consecutive processing errors, switches to read-only mode |
| **Portfolio health gate** | Checks drawdown, daily loss, exposure — pauses strategy on breach |
| **Drawdown alert**        | One-time alert when drawdown exceeds configured threshold         |
| **Data source guard**     | Prevents calling `start()` more than once                         |

## Runner Functions

Module: `coinjure.engine.runner`

High-level functions that wire up the engine with appropriate data sources and traders.

```python
from coinjure.engine.runner import (
    run_live_paper_trading,
    run_live_polymarket_trading,
)
```

### run_live_paper_trading

```python
await run_live_paper_trading(
    strategy=strategy,
    exchange="polymarket",
    initial_capital=Decimal("10000"),
    data_dir=Path("./data"),
)
```

Sets up `PaperTrader` + live data sources + `StandardRiskManager` + `StateStore` for simulated trading against live feeds.

### run_live_polymarket_trading / run_live_kalshi_trading

Same architecture but with real traders that submit orders to exchanges. Requires API credentials.

## PerformanceAnalyzer

Module: `coinjure.engine.performance`

Tracks all trades and computes performance metrics.

| Metric        | Description                         |
| ------------- | ----------------------------------- |
| Total PnL     | Sum of realized + unrealized P&L    |
| Win Rate      | Winning trades / total trades       |
| Sharpe Ratio  | Annualized (252 trading days)       |
| Max Drawdown  | Peak-to-trough as percentage        |
| Profit Factor | Gross profit / gross loss           |
| Equity Curve  | Portfolio value at each trade point |

## ControlServer

Module: `coinjure.engine.control`

Unix socket RPC server running inside the engine process. Enables external control without restarting.

| Command         | Effect                                         |
| --------------- | ---------------------------------------------- |
| `pause`         | Stop data ingestion and decisions              |
| `resume`        | Restart data ingestion and decisions           |
| `stop`          | Graceful engine shutdown                       |
| `status`        | Quick stats (runtime, events, P&L)             |
| `get_state`     | Full snapshot (positions, orders, books, news) |
| `swap_strategy` | Hot-swap strategy class without restart        |

```python
from coinjure.engine.control import send_command

result = await send_command("status", socket_path)
result = await send_command("pause", socket_path)
result = await send_command("swap_strategy", socket_path,
                            strategy_ref="coinjure.strategy.demo:DemoStrategy")
```
