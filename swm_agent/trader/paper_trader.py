import random
from decimal import Decimal

from position.position_manager import PositionManager
from risk.risk_manager import RiskManager
from ticker.ticker import Ticker

from data.market_data_manager import MarketDataManager

from .trader import Trader
from .types import (
    Fill,
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
        self.trades: list[Trade] = []
        self.min_fill_rate = min_fill_rate
        self.max_fill_rate = max_fill_rate
        self.commission_rate = commission_rate

    def _simulate_execution(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> Trade | None:
        """Simulate order execution based on current market data
        Returns:
            Trade if there is any fill
            None if no fill
        """
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
            return None

        # Pessimistic fill it at the limit price
        fills: list[Fill] = []
        fills.append(Fill(price=limit_price, quantity=filled_qty))

        remaining = quantity - filled_qty
        commission = filled_qty * limit_price * self.commission_rate

        return Trade(
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            filled_quantity=filled_qty,
            average_price=limit_price,
            fills=fills,
            remaining=remaining,
            commission=commission,
        )

    async def place_order(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> PlaceOrderResult:
        # Validate inputs
        if quantity <= 0 or limit_price <= 0:
            return PlaceOrderResult(
                status=OrderStatus.REJECTED,
                failure_reason=OrderFailureReason.INVALID_ORDER,
            )

        # Don't allow short selling
        if side == TradeSide.SELL:
            position = self.position_manager.get_position(ticker)
            if position is None or position.quantity < quantity:
                return PlaceOrderResult(
                    status=OrderStatus.REJECTED,
                    failure_reason=OrderFailureReason.INVALID_ORDER,
                )

        # TODO: check if we have enough cash

        # Check risk limits
        if not self.risk_manager.check_trade(
            ticker.symbol, side, quantity, limit_price
        ):
            return PlaceOrderResult(
                status=OrderStatus.REJECTED,
                failure_reason=OrderFailureReason.RISK_CHECK_FAILED,
            )

        # Execute order
        trade = self._simulate_execution(side, ticker, limit_price, quantity)

        # No fill
        if trade is None:
            return PlaceOrderResult(status=OrderStatus.PLACED, trade=None)

        # Update position
        self.position_manager.update_position(
            ticker,
            trade.filled_quantity if side == TradeSide.BUY else -trade.filled_quantity,
            trade.average_price,
        )
        # TODO: Update cash balance

        # Store trade
        self.trades.append(trade)

        if trade.filled_quantity == quantity:
            return PlaceOrderResult(status=OrderStatus.FILLED, trade=trade)
        else:
            return PlaceOrderResult(
                status=OrderStatus.PARTIALLY_FILLED,
                trade=trade,
            )
