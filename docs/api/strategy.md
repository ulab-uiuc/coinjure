# Strategy

Module: `coinjure.strategy.strategy`

The `Strategy` ABC is the base class for all trading strategies. Subclasses implement `process_event` to react to market data and place orders.

## Strategy

```python
from coinjure.strategy.strategy import Strategy
from coinjure.events import Event
from coinjure.trading.trader import Trader

class MyStrategy(Strategy):
    name = "my_strategy"
    version = "0.1.0"

    async def process_event(self, event: Event, trader: Trader) -> None:
        ctx = self.require_context()
        # Your logic here
```

### Class Variables

| Field           | Type  | Description                         |
| --------------- | ----- | ----------------------------------- |
| `name`          | `str` | Strategy display name               |
| `version`       | `str` | Semantic version string             |
| `author`        | `str` | Author identifier                   |
| `strategy_type` | `str` | Category tag (default: `"generic"`) |

### Methods

| Method                                      | Description                                                |
| ------------------------------------------- | ---------------------------------------------------------- |
| `process_event(event, trader)`              | **Abstract.** Handle one market event.                     |
| `on_start()`                                | Lifecycle hook — called before the first event.            |
| `on_stop()`                                 | Lifecycle hook — called after the last event.              |
| `watch_tokens() -> list[str]`               | Return token IDs the data source should prioritize.        |
| `set_paused(paused)`                        | Pause/resume decision-making via control plane.            |
| `is_paused() -> bool`                       | Check if strategy is paused.                               |
| `is_warming_up() -> bool`                   | Check if still in warmup window after engine start.        |
| `bind_context(event, trader)`               | Bind `StrategyContext` for this timestep.                  |
| `require_context() -> StrategyContext`      | Get bound context or raise `RuntimeError`.                 |
| `record_decision(...)`                      | Record a decision to the shared buffer + counters.         |
| `get_decisions() -> list[StrategyDecision]` | Return recent decisions (max 200).                         |
| `get_decision_stats() -> dict`              | Return running decision counters.                          |
| `param_schema() -> dict`                    | Classmethod. Introspect `__init__` for tunable parameters. |
| `reset_live_state()`                        | Reset ephemeral state between walk-forward phases.         |

## StrategyContext

Unified runtime context bound before every strategy invocation. Provides safe access to market data, positions, and news without touching internal engine state.

```python
ctx = self.require_context()

# Market data
books = ctx.order_books(limit=10)
history = ctx.ticker_history(limit=50)
prices = ctx.price_history(limit=100)
tickers = ctx.available_tickers()

# Positions
positions = ctx.active_positions()
cash = ctx.cash_positions()

# News
news = ctx.recent_news(limit=5)

# Ticker resolution
ticker = ctx.resolve_ticker("TRUMP_YES")
trade_ticker = ctx.resolve_trade_ticker("TRUMP", side="no")
```

### Properties

| Property          | Type             | Description                                         |
| ----------------- | ---------------- | --------------------------------------------------- |
| `event`           | `Event`          | The current event being processed                   |
| `trader`          | `Trader`         | The active trader instance                          |
| `event_type`      | `str`            | Class name of the event (e.g. `"PriceChangeEvent"`) |
| `ticker`          | `Ticker \| None` | Ticker from the event, if present                   |
| `event_timestamp` | `object`         | Timestamp or published_at from the event            |

### Methods

| Method                                          | Returns                       | Description                            |
| ----------------------------------------------- | ----------------------------- | -------------------------------------- |
| `market_history(ticker, limit)`                 | `list[DataPoint]`             | Full market history for a ticker       |
| `ticker_history(limit)`                         | `list[DataPoint]`             | History for the current event's ticker |
| `price_history(ticker, limit)`                  | `list`                        | Price-only history                     |
| `order_books(limit)`                            | `list[StrategyOrderBookView]` | Current order book snapshots           |
| `available_tickers(limit, include_complements)` | `list[Ticker]`                | All tickers with active order books    |
| `resolve_ticker(symbol)`                        | `Ticker \| None`              | Find ticker by symbol string           |
| `resolve_trade_ticker(symbol, side)`            | `Ticker \| None`              | Find ticker for YES/NO side            |
| `positions()`                                   | `list[StrategyPositionView]`  | All positions (cash + market)          |
| `cash_positions()`                              | `list[StrategyPositionView]`  | Cash-only positions                    |
| `active_positions()`                            | `list[StrategyPositionView]`  | Non-cash positions with quantity > 0   |
| `recent_news(limit)`                            | `list[dict]`                  | Recent news headlines                  |

## StrategyDecision

Dataclass emitted by `record_decision()`.

| Field           | Type               | Description                                  |
| --------------- | ------------------ | -------------------------------------------- |
| `timestamp`     | `str`              | `HH:MM:SS` format                            |
| `ticker_name`   | `str`              | Market name (truncated to 40 chars)          |
| `action`        | `str`              | `BUY_YES`, `BUY_NO`, `HOLD`, `CLOSE_*`, etc. |
| `executed`      | `bool`             | Whether the order was actually placed        |
| `reasoning`     | `str`              | Human-readable explanation                   |
| `confidence`    | `float`            | Signal confidence (0.0 if N/A)               |
| `signal_values` | `dict[str, float]` | Strategy-specific signals                    |

## Built-in Strategies

All built-in strategies live in `coinjure.strategy.builtin` and are auto-selected by relation type:

| Strategy                 | Relation                       | Module                             |
| ------------------------ | ------------------------------ | ---------------------------------- |
| `DirectArbStrategy`      | `same_event`                   | `builtin.direct_arb_strategy`      |
| `GroupArbStrategy`       | `complementary`, `exclusivity` | `builtin.group_arb_strategy`       |
| `ImplicationArbStrategy` | `implication`                  | `builtin.implication_arb_strategy` |
| `CointSpreadStrategy`    | `correlated`                   | `builtin.coint_spread_strategy`    |
| `StructuralArbStrategy`  | `structural`                   | `builtin.structural_arb_strategy`  |
| `ConditionalArbStrategy` | `conditional`                  | `builtin.conditional_arb_strategy` |
| `LeadLagStrategy`        | `temporal`                     | `builtin.lead_lag_strategy`        |

## Loading Strategies

Strategies are referenced by `module.path:ClassName` or `/path/file.py:ClassName`:

```python
from coinjure.strategy.loader import load_strategy

strategy = load_strategy("coinjure.strategy.demo:DemoStrategy")
strategy = load_strategy("./my_strategies/alpha.py:AlphaStrategy")
```
