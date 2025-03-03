from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ticker.ticker import Ticker


class TradeSide(Enum):
    BUY = 'buy'
    SELL = 'sell'


@dataclass
class Trade:
    side: TradeSide
    ticker: Ticker
    price: Decimal
    quantity: Decimal
    commission: Decimal


class OrderStatus(Enum):
    PLACED = 'placed'
    REJECTED = 'rejected'
    PARTIALLY_FILLED = 'partially_filled'
    FILLED = 'filled'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'


class OrderFailureReason(Enum):
    RISK_CHECK_FAILED = 'risk_check_failed'
    INVALID_ORDER = 'invalid_order'
    INSUFFICIENT_CASH = 'insufficient_cash'
    UNKNOWN = 'unknown'


@dataclass
class Order:
    status: OrderStatus
    side: TradeSide
    ticker: Ticker
    limit_price: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    trades: list[Trade]
    remaining: Decimal
    commission: Decimal


@dataclass
class PlaceOrderResult:
    order: Order | None = None
    failure_reason: OrderFailureReason | None = None
