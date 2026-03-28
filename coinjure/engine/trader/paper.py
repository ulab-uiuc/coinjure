from __future__ import annotations

import random
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from coinjure.data.manager import DataManager
from coinjure.ticker import Ticker
from coinjure.trading.position import PositionManager
from coinjure.trading.risk import RiskManager
from coinjure.trading.trader import Trader
from coinjure.trading.types import (
    Order,
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    Trade,
    TradeSide,
)

if TYPE_CHECKING:
    from coinjure.engine.trader.alerter import Alerter

_RESTING_ORDER_TTL = 300.0  # Cancel unfilled resting orders after 5 minutes


class PaperTrader(Trader):
    def __init__(
        self,
        market_data: DataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        min_fill_rate: Decimal,
        max_fill_rate: Decimal,
        commission_rate: Decimal = Decimal('0.02'),
        alerter: Alerter | None = None,
        slippage_bps: int = 50,
    ):
        super().__init__(market_data, risk_manager, position_manager, alerter=alerter)
        self.orders: list[Order] = []
        self._resting_orders: list[Order] = []  # Only unfilled/partial orders
        self.min_fill_rate = min_fill_rate
        self.max_fill_rate = max_fill_rate
        self.commission_rate = commission_rate
        self.slippage_bps = slippage_bps
        self._slippage_factor = Decimal(str(slippage_bps)) / Decimal('10000')

    def _random_fill_rate(self) -> Decimal:
        fill_range = float(self.max_fill_rate) - float(self.min_fill_rate)
        if fill_range <= 0:
            return self.min_fill_rate
        # Beta(5,1) distribution: most fills near 100%, rare low fills
        beta_sample = random.betavariate(5, 1)
        return Decimal(
            str(float(self.min_fill_rate) + beta_sample * fill_range)
        )

    def _available_liquidity(
        self,
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        fill_rate: Decimal | None = None,
    ) -> Decimal:
        """Compute available liquidity within the price limit, scaled by fill rate."""
        levels = (
            self.market_data.get_asks(ticker)
            if side == TradeSide.BUY
            else self.market_data.get_bids(ticker)
        )
        raw = Decimal('0')
        for level in levels:
            if (side == TradeSide.BUY and level.price <= limit_price) or (
                side == TradeSide.SELL and level.price >= limit_price
            ):
                raw += Decimal(level.size)

        if fill_rate is None:
            fill_rate = self._random_fill_rate()
        return raw * fill_rate

    def _simulate_execution(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> Order:
        """Simulate order execution based on current market data."""
        # Generate fill rate once per order — reused for resting fill attempts
        fill_rate = self._random_fill_rate()
        available = self._available_liquidity(side, ticker, limit_price, fill_rate)
        filled_qty = min(quantity, available)

        if filled_qty == Decimal('0'):
            order = Order(
                status=OrderStatus.PLACED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=Decimal('0'),
                average_price=Decimal('0'),
                trades=[],
                remaining=quantity,
                commission=Decimal('0'),
            )
            order._fill_rate = fill_rate  # type: ignore[attr-defined]
            order._created_at = time.monotonic()  # type: ignore[attr-defined]
            return order

        # Apply slippage: BUY fills worse (higher), SELL fills worse (lower)
        if side == TradeSide.BUY:
            fill_price = limit_price * (Decimal('1') + self._slippage_factor)
        else:
            fill_price = limit_price * (Decimal('1') - self._slippage_factor)

        commission = filled_qty * fill_price * self.commission_rate
        trades: list[Trade] = [
            Trade(
                side=side,
                ticker=ticker,
                price=fill_price,
                quantity=filled_qty,
                commission=commission,
            )
        ]

        remaining = quantity - filled_qty
        order = Order(
            status=OrderStatus.FILLED
            if remaining == Decimal('0')
            else OrderStatus.PARTIALLY_FILLED,
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            filled_quantity=filled_qty,
            average_price=fill_price,
            trades=trades,
            remaining=remaining,
            commission=commission,
        )
        order._fill_rate = fill_rate  # type: ignore[attr-defined]
        order._created_at = time.monotonic()  # type: ignore[attr-defined]
        return order

    def try_fill_resting_orders(self) -> None:
        """Attempt to fill resting (PLACED/PARTIALLY_FILLED) orders against current orderbook."""
        if not self._resting_orders:
            return
        now = time.monotonic()
        still_resting: list[Order] = []
        for order in self._resting_orders:
            # Expire stale resting orders
            created = getattr(order, '_created_at', now)
            if now - created > _RESTING_ORDER_TTL:
                order.status = OrderStatus.CANCELLED
                continue
            # Reuse the fill rate assigned at order creation for determinism
            fill_rate = getattr(order, '_fill_rate', None)
            available = self._available_liquidity(
                order.side,
                order.ticker,
                order.limit_price,
                fill_rate,
            )
            filled_qty = min(order.remaining, available)
            if filled_qty <= 0:
                still_resting.append(order)
                continue

            if order.side == TradeSide.BUY:
                resting_fill_price = order.limit_price * (Decimal('1') + self._slippage_factor)
            else:
                resting_fill_price = order.limit_price * (Decimal('1') - self._slippage_factor)

            trade = Trade(
                side=order.side,
                ticker=order.ticker,
                price=resting_fill_price,
                quantity=filled_qty,
                commission=filled_qty * resting_fill_price * self.commission_rate,
            )
            self.position_manager.apply_trade(trade)
            order.trades.append(trade)
            order.filled_quantity += filled_qty
            order.remaining -= filled_qty
            order.average_price = resting_fill_price
            order.commission += trade.commission
            if order.remaining == Decimal('0'):
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
                still_resting.append(order)
        self._resting_orders = still_resting

    async def _alert_rejected(self, reason: OrderFailureReason, ticker: Ticker) -> None:
        if self.alerter:
            try:
                await self.alerter.on_order_rejected(reason, ticker)
            except Exception:
                pass

    async def place_order(
        self,
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> PlaceOrderResult:
        guard_failure = self._check_order_guard(client_order_id)
        if guard_failure is not None:
            await self._alert_rejected(guard_failure, ticker)
            return PlaceOrderResult(order=None, failure_reason=guard_failure)

        if not self.is_ticker_tradable(ticker):
            await self._alert_rejected(OrderFailureReason.MARKET_NOT_ALLOWED, ticker)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.MARKET_NOT_ALLOWED,
            )

        # Validate inputs
        if quantity <= 0 or limit_price <= 0:
            await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.INVALID_ORDER,
            )

        # Don't allow short selling
        if side == TradeSide.SELL:
            position = self.position_manager.get_position(ticker)
            if position is None or position.quantity < quantity:
                await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
                return PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INVALID_ORDER,
                )

        # Check if we have enough cash
        if side == TradeSide.BUY:
            cash_position = self.position_manager.get_position(ticker.collateral)
            cash_required = quantity * limit_price * (1 + self.commission_rate)
            if cash_position is None or cash_position.quantity < cash_required:
                await self._alert_rejected(OrderFailureReason.INSUFFICIENT_CASH, ticker)
                return PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INSUFFICIENT_CASH,
                )

        # Check risk limits
        if not await self.risk_manager.check_trade(ticker, side, quantity, limit_price):
            await self._alert_rejected(OrderFailureReason.RISK_CHECK_FAILED, ticker)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.RISK_CHECK_FAILED,
            )

        # Execute order
        order = self._simulate_execution(side, ticker, limit_price, quantity)

        # Update position
        for trade in order.trades:
            self.position_manager.apply_trade(trade)

        # Store order
        self.orders.append(order)
        if order.status in (OrderStatus.PLACED, OrderStatus.PARTIALLY_FILLED):
            self._resting_orders.append(order)

        return PlaceOrderResult(order=order)
