from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List

from ticker.ticker import Ticker


class TradeSide(Enum):
    BUY = "buy"
    SELL = "sell"

@dataclass
class Fill:
    price: Decimal
    quantity: Decimal

@dataclass
class Trade:
    side: TradeSide
    ticker: Ticker
    limit_price: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    fills: List[Fill]
    remaining: Decimal
    commission: Decimal

class OrderStatus(Enum):
    PLACED = "placed"
    REJECTED = "rejected"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

class OrderFailureReason(Enum):
    RISK_CHECK_FAILED = "risk_check_failed"
    INVALID_ORDER = "invalid_order"
    UNKNOWN = "unknown"

@dataclass
class PlaceOrderResult:
    status: OrderStatus
    trade: Trade | None = None
    failure_reason: OrderFailureReason | None = None
