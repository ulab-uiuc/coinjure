# API Reference

Coinjure's Python API is organized into a few key modules. Most users interact with Coinjure through the CLI, but the Python API is useful for writing custom strategies or extending the system programmatically.

## Modules

- [Strategy](strategy.md) — `Strategy` ABC, `StrategyContext`, decision recording
- [Trading Types](trading.md) — `Order`, `Trade`, `TradeSide`, `PlaceOrderResult`
- [Data Sources](data.md) — `DataSource` ABC, `CompositeDataSource`, event streams
- [Market Relations](relations.md) — `MarketRelation`, `RelationStore`, 8 relation types
- [Engine](engine.md) — `TradingEngine`, runner functions, `ControlServer`
