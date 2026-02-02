import random
from decimal import Decimal

from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import PositionManager
from swm_agent.risk.risk_manager import RiskManager
from swm_agent.ticker.ticker import Ticker

from .trader import Trader
from .types import (
    Order,
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    Trade,
    TradeSide,
)


class PaperTrader(Trader):
    def __init__(
        self,
        market_data: MarketDataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        min_fill_rate: Decimal,
        max_fill_rate: Decimal,
        commission_rate: Decimal,
    ):
        super().__init__(market_data, risk_manager, position_manager)
        self.orders: list[Order] = []
        self.min_fill_rate = min_fill_rate
        self.max_fill_rate = max_fill_rate
        self.commission_rate = commission_rate

    def _simulate_execution(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> Order:
        """Simulate order execution based on current market data"""
        levels = (
            self.market_data.get_asks(ticker)
            if side == TradeSide.BUY
            else self.market_data.get_bids(ticker)
        )

        # Calculate available liquidity within our price limit
        available_liquidity = Decimal('0.0')

        for level in levels:
            if (side == TradeSide.BUY and level.price <= limit_price) or (
                side == TradeSide.SELL and level.price >= limit_price
            ):
                # Ensure addition is with Decimal
                available_liquidity += Decimal(level.size)

        # Simulate that not all liquidity is actually available
        fill_rate = Decimal(
            str(random.uniform(float(self.min_fill_rate), float(self.max_fill_rate)))
        )
        available_liquidity *= fill_rate

        # Calculate how much we can fill
        filled_qty = min(quantity, available_liquidity)
        if filled_qty == Decimal('0.0'):
            # TODO: record orders for future matching
            return Order(
                status=OrderStatus.PLACED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=Decimal('0.0'),
                average_price=Decimal('0.0'),
                trades=[],
                remaining=quantity,
                commission=Decimal('0.0'),
            )

        # Pessimistic fill it at the limit price
        trades: list[Trade] = [
            Trade(
                side=side,
                ticker=ticker,
                price=limit_price,
                quantity=filled_qty,
                commission=filled_qty * limit_price * self.commission_rate,
            )
        ]

        remaining = quantity - filled_qty
        commission = sum(trade.commission for trade in trades)

        return Order(
            status=OrderStatus.FILLED
            if remaining == Decimal('0.0')
            else OrderStatus.PARTIALLY_FILLED,
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            filled_quantity=filled_qty,
            average_price=limit_price,
            trades=trades,
            remaining=remaining,
            commission=commission,
        )

    async def place_order(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> PlaceOrderResult:
        # Validate inputs
        if quantity <= 0 or limit_price <= 0:
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.INVALID_ORDER,
            )

        # Don't allow short selling
        if side == TradeSide.SELL:
            position = self.position_manager.get_position(ticker)
            if position is None or position.quantity < quantity:
                return PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INVALID_ORDER,
                )

        # Check if we have enough cash
        if side == TradeSide.BUY:
            cash_position = self.position_manager.get_position(ticker.collateral)
            cash_required = quantity * limit_price * (1 + self.commission_rate)
            if cash_position is None or cash_position.quantity < cash_required:
                return PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INSUFFICIENT_CASH,
                )

        # Check risk limits
        if not await self.risk_manager.check_trade(ticker, side, quantity, limit_price):
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

        return PlaceOrderResult(order=order)
