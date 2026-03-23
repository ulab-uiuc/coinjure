# Trading Types

Module: `coinjure.trading.types`

Core trading primitives shared across strategies, traders, and the engine.

## TradeSide

```python
from coinjure.trading.types import TradeSide

TradeSide.BUY   # 'buy'
TradeSide.SELL   # 'sell'
```

## Trade

A single executed fill.

```python
from coinjure.trading.types import Trade

trade = Trade(
    side=TradeSide.BUY,
    ticker=my_ticker,
    price=Decimal("0.65"),
    quantity=Decimal("100"),
    commission=Decimal("0.50"),
)
```

| Field        | Type        | Description        |
| ------------ | ----------- | ------------------ |
| `side`       | `TradeSide` | Buy or sell        |
| `ticker`     | `Ticker`    | The market ticker  |
| `price`      | `Decimal`   | Execution price    |
| `quantity`   | `Decimal`   | Filled quantity    |
| `commission` | `Decimal`   | Commission charged |

## Order

An order with its execution state.

```python
from coinjure.trading.types import Order, OrderStatus
```

| Field             | Type          | Description                |
| ----------------- | ------------- | -------------------------- |
| `status`          | `OrderStatus` | Current order state        |
| `side`            | `TradeSide`   | Buy or sell                |
| `ticker`          | `Ticker`      | Target market              |
| `limit_price`     | `Decimal`     | Limit price                |
| `filled_quantity` | `Decimal`     | Total quantity filled      |
| `average_price`   | `Decimal`     | Volume-weighted fill price |
| `trades`          | `list[Trade]` | Individual fills           |
| `remaining`       | `Decimal`     | Unfilled quantity          |
| `commission`      | `Decimal`     | Total commission           |

## OrderStatus

```python
from coinjure.trading.types import OrderStatus

OrderStatus.PLACED            # Submitted
OrderStatus.REJECTED          # Failed pre-trade checks
OrderStatus.PARTIALLY_FILLED  # Some fills, remainder resting
OrderStatus.FILLED            # Fully executed
OrderStatus.CANCELLED         # Cancelled
OrderStatus.UNKNOWN           # Indeterminate state
```

## OrderFailureReason

```python
from coinjure.trading.types import OrderFailureReason

OrderFailureReason.RISK_CHECK_FAILED   # Risk manager rejected
OrderFailureReason.INVALID_ORDER       # Malformed order
OrderFailureReason.INSUFFICIENT_CASH   # Not enough balance
OrderFailureReason.DUPLICATE_ORDER     # Duplicate detection
OrderFailureReason.MARKET_NOT_ALLOWED  # Ticker not tradable
OrderFailureReason.TRADING_DISABLED    # Kill-switch or read-only
OrderFailureReason.UNKNOWN
```

## PlaceOrderResult

Returned by `Trader.place_order()`. Wraps the order or a failure reason.

```python
from coinjure.trading.types import PlaceOrderResult

result = await trader.place_order(TradeSide.BUY, ticker, price, quantity)

if result.accepted:
    print(f"Order placed: {result.order.status}")
if result.executed:
    print(f"Filled {result.order.filled_quantity} @ {result.order.average_price}")
if result.failure_reason:
    print(f"Failed: {result.failure_reason.value}")
```

| Property         | Type                         | Description                            |
| ---------------- | ---------------------------- | -------------------------------------- |
| `order`          | `Order \| None`              | The order, if accepted                 |
| `failure_reason` | `OrderFailureReason \| None` | Why it failed, if rejected             |
| `status`         | `str`                        | `"failed"` or the order's status value |
| `accepted`       | `bool`                       | `True` if order was not rejected       |
| `executed`       | `bool`                       | `True` if any quantity was filled      |
