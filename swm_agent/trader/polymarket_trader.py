import asyncio
import logging
from decimal import Decimal
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL
from py_order_utils.model import EOA

from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import RiskManager
from swm_agent.ticker.ticker import CashTicker, PolyMarketTicker, Ticker
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import (
    Order,
    OrderFailureReason,
    OrderStatus,
    PlaceOrderResult,
    Trade,
    TradeSide,
)

logger = logging.getLogger(__name__)


class PolymarketTrader(Trader):
    def __init__(
        self,
        market_data: MarketDataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        wallet_private_key: str,
        signature_type: int = EOA,
        funder: str = None,
        clob_api_url: str = 'https://clob.polymarket.com',
        chain_id: int = POLYGON,
        commission_rate: Decimal = Decimal('0.0'),
    ):
        super().__init__(market_data, risk_manager, position_manager)

        self.commission_rate = commission_rate

        self.clob_client = ClobClient(
            clob_api_url,
            key=wallet_private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )

        # get api credentials
        self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())

        self.orders: list[Order] = []

    def _sync_usdc_balance(self) -> None:
        """Re-fetch USDC balance from Polymarket and update position manager."""
        try:
            balance_info = self.clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            actual_balance = Decimal(balance_info['balance']) / Decimal('1000000')
            existing = self.position_manager.get_position(CashTicker.POLYMARKET_USDC)
            self.position_manager.update_position(
                Position(
                    ticker=CashTicker.POLYMARKET_USDC,
                    quantity=actual_balance,
                    average_cost=Decimal('1'),
                    realized_pnl=existing.realized_pnl if existing else Decimal('0'),
                )
            )
        except Exception:
            pass  # Non-critical — local tracking continues as fallback

    async def _submit_fok_order(
        self, side: TradeSide, ticker: PolyMarketTicker, price: Decimal, size: Decimal
    ) -> dict[str, Any]:
        if not ticker.token_id:
            raise ValueError('Ticker must have a valid token_id')

        clob_side = BUY if side == TradeSide.BUY else SELL

        order_args = OrderArgs(
            price=float(price),
            size=float(size),
            side=clob_side,
            token_id=ticker.token_id,
        )

        order = self.clob_client.create_order(order_args)
        response = self.clob_client.post_order(order, orderType=OrderType.FOK)

        return response

    async def _get_order_status(self, order_id: str) -> dict[str, Any]:
        return self.clob_client.get_order(order_id)

    async def _process_order_response(
        self,
        response: dict[str, Any],
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> Order:
        logger.debug('Order response: %s', response)
        # Check if order was successfully submitted
        if not response.get('success'):
            return Order(
                status=OrderStatus.REJECTED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=Decimal('0'),
                average_price=Decimal('0'),
                trades=[],
                remaining=quantity,
                commission=Decimal('0'),
            )

        order_id = response.get('orderID')
        if not order_id:
            return Order(
                status=OrderStatus.REJECTED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=Decimal('0'),
                average_price=Decimal('0'),
                trades=[],
                remaining=quantity,
                commission=Decimal('0'),
            )

        # Get order status
        order_details = await self._get_order_status(order_id)
        logger.debug('Order details: %s', order_details)

        # Since we only submit FOK order now, if the order is not matched, consider it rejected
        if (
            not order_details
            or 'status' not in order_details
            or order_details['status'] != 'MATCHED'
        ):
            return Order(
                status=OrderStatus.REJECTED,
                side=side,
                ticker=ticker,
                limit_price=limit_price,
                filled_quantity=Decimal('0'),
                average_price=Decimal('0'),
                trades=[],
                remaining=quantity,
                commission=Decimal('0'),
            )

        # fill details, we ignore polymarket's trades for now,
        # and consider one order has one trade
        filled_size = Decimal(str(order_details.get('size_matched', '0')))
        # calculate avg fill price for FOK order
        # if the fill price is better than limit price, polymarket will fill
        # more than original quantity
        filled_price = limit_price * quantity / filled_size
        # remaining = quantity - filled_size
        # for FOK, remaining must be 0. filled_size might be greater than quantity
        remaining = Decimal('0')
        commission = filled_size * filled_price * self.commission_rate

        # Create the trade
        trade = Trade(
            side=side,
            ticker=ticker,
            price=filled_price,
            quantity=filled_size,
            commission=commission,
        )

        return Order(
            status=OrderStatus.FILLED,
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            filled_quantity=filled_size,
            average_price=filled_price,
            trades=[trade],
            remaining=remaining,
            commission=commission,
        )

    async def place_order(  # noqa: C901
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> PlaceOrderResult:
        # Validate inputs
        if quantity <= 0 or limit_price <= 0:
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.INVALID_ORDER,
            )

        # Verify ticker is a PolyMarketTicker with token_id
        if not isinstance(ticker, PolyMarketTicker) or not ticker.token_id:
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
            cash_required = (
                quantity * limit_price * (Decimal('1') + self.commission_rate)
            )
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

        try:
            # Submit the FOK order
            response = await self._submit_fok_order(side, ticker, limit_price, quantity)

            # Process the response
            order = await self._process_order_response(
                response, side, ticker, limit_price, quantity
            )

            # update positions
            for trade in order.trades:
                self.position_manager.apply_trade(trade)
            self._sync_usdc_balance()

            # Store order
            self.orders.append(order)

            return PlaceOrderResult(order=order)

        except PolyApiException as e:
            logger.error('Polymarket API error: %s', e)
            failure_reason = OrderFailureReason.UNKNOWN
            if e.status_code == 400:
                error_msg = str(e.error_msg).lower()
                if 'insufficient' in error_msg or 'balance' in error_msg:
                    failure_reason = OrderFailureReason.INSUFFICIENT_CASH
                else:
                    failure_reason = OrderFailureReason.INVALID_ORDER
            return PlaceOrderResult(order=None, failure_reason=failure_reason)
        except Exception as e:
            logger.error('Error placing order: %s', e, exc_info=True)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.UNKNOWN,
            )


if __name__ == '__main__':
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    from py_order_utils.model import POLY_GNOSIS_SAFE

    from swm_agent.position.position_manager import Position
    from swm_agent.risk.risk_manager import NoRiskManager
    from swm_agent.ticker.ticker import CashTicker

    async def test_polymarket_trader():
        trader = PolymarketTrader(
            market_data=MarketDataManager(),
            risk_manager=NoRiskManager(),
            position_manager=PositionManager(),
            wallet_private_key='<>',
            signature_type=POLY_GNOSIS_SAFE,
            funder='<>',
        )
        balance_info = trader.clob_client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(balance_info)
        trader.position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal(balance_info['balance']) / Decimal('1000000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        print(trader.position_manager.get_cash_positions())
        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=PolyMarketTicker(
                symbol='<>', name='<>', token_id='<>', market_id='<>', event_id='<>'
            ),
            limit_price=Decimal('0.01'),
            quantity=Decimal('100'),
        )
        print(result)
        print(trader.position_manager.get_cash_positions())
        print(trader.position_manager.get_non_cash_positions())

    asyncio.run(test_polymarket_trader())
    print('test completed')
