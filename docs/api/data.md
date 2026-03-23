# Data Sources

Module: `coinjure.data.source`

Data sources provide the event stream that drives the trading engine. All sources implement the `DataSource` ABC.

## DataSource

```python
from coinjure.data.source import DataSource
from coinjure.events import Event

class MyDataSource(DataSource):
    async def get_next_event(self) -> Event | None:
        """Return the next event, or None if exhausted/timeout."""
        ...

    async def start(self) -> None:
        """Called once before the engine begins polling."""
        ...

    async def stop(self) -> None:
        """Called when the engine shuts down."""
        ...
```

| Method             | Description                                                 |
| ------------------ | ----------------------------------------------------------- |
| `get_next_event()` | **Abstract.** Return next event or `None`.                  |
| `start()`          | Lifecycle hook — launch background tasks, open connections. |
| `stop()`           | Lifecycle hook — cancel tasks, close connections.           |

## CompositeDataSource

Merges events from multiple `DataSource` instances into a single stream via an internal queue.

```python
from coinjure.data.source import CompositeDataSource

source = CompositeDataSource([poly_source, kalshi_source, news_source])
await source.start()  # starts all children + relay tasks
event = await source.get_next_event()  # pulls from merged queue
```

| Method                                    | Description                             |
| ----------------------------------------- | --------------------------------------- |
| `drain_pending_events()`                  | Non-blocking drain of all queued events |
| `register_token_ticker(token_id, ticker)` | Forward token registration to children  |
| `watch_token(token_id)`                   | Subscribe a token across all children   |
| `unwatch_token(token_id)`                 | Unsubscribe a token across all children |

## build_market_source

Factory function that builds a live data source for a given exchange.

```python
from coinjure.data.source import build_market_source

source = build_market_source("polymarket")      # Polymarket CLOB + RSS news
source = build_market_source("kalshi")           # Kalshi REST + RSS news
source = build_market_source("cross_platform")   # Both exchanges + RSS news
```

| Exchange           | Sources                                              |
| ------------------ | ---------------------------------------------------- |
| `"polymarket"`     | `LivePolyMarketDataSource` + `LiveRSSNewsDataSource` |
| `"kalshi"`         | `LiveKalshiDataSource` + `LiveRSSNewsDataSource`     |
| `"cross_platform"` | Both live sources + `LiveRSSNewsDataSource`          |

## Concrete Implementations

### LivePolyMarketDataSource

Module: `coinjure.data.live.polymarket`

Connects to Polymarket's CLOB WebSocket for real-time order book updates and polls the Gamma API for market discovery.

### LiveKalshiDataSource

Module: `coinjure.data.live.kalshi`

Polls Kalshi's REST API for market snapshots and order book data.

### ParquetDataSource

Module: `coinjure.data.backtest.parquet`

Replays historical orderbook snapshots from Parquet files for backtesting. Supports multi-file concatenation and market ID filtering.

```python
from coinjure.data.backtest.parquet import ParquetDataSource

source = ParquetDataSource(
    parquet_paths=["data/orderbook_2026-03-05T02.parquet"],
    market_ids=["12345", "67890"],  # optional filter
)
```

## Event Types

All data sources emit events from `coinjure.events`:

| Event              | Fields                                      | Description              |
| ------------------ | ------------------------------------------- | ------------------------ |
| `PriceChangeEvent` | `ticker`, `bid`, `ask`, `timestamp`         | Price update             |
| `OrderBookEvent`   | `ticker`, `bids`, `asks`, `timestamp`       | Full order book snapshot |
| `NewsEvent`        | `headline`, `source`, `url`, `published_at` | News article             |
