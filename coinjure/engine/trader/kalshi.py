from __future__ import annotations

import asyncio
import logging
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from coinjure.data.manager import DataManager
from coinjure.ticker import KalshiTicker, Ticker
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

logger = logging.getLogger(__name__)


class KalshiTrader(Trader):
    def __init__(
        self,
        market_data: DataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        commission_rate: Decimal = Decimal('0.0'),
        alerter: Alerter | None = None,
    ):
        super().__init__(market_data, risk_manager, position_manager, alerter=alerter)
        self.commission_rate = commission_rate

        from kalshi_python import Configuration
        from kalshi_python.api.portfolio_api import PortfolioApi
        from kalshi_python.api_client import ApiClient

        config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')

        key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
        pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')

        if not key_id or not pk_path:
            raise ValueError(
                'Kalshi API credentials required. '
                'Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars '
                'or pass api_key_id and private_key_path.'
            )

        self._api_client = ApiClient(configuration=config)
        self._api_client.set_kalshi_auth(key_id, pk_path)
        self._portfolio_api = PortfolioApi(self._api_client)
        self.orders: list[Order] = []

    async def _submit_order(
        self,
        action: str,
        side: str,
        ticker: KalshiTicker,
        price_cents: int,
        count: int,
    ) -> dict[str, Any]:
        """Submit an order to Kalshi API."""
        kwargs: dict[str, Any] = {
            'ticker': ticker.market_ticker,
            'action': action,
            'side': side,
            'type': 'limit',
            'count': count,
            'client_order_id': str(uuid.uuid4()),
        }
        if side == 'no':
            kwargs['no_price'] = price_cents
        else:
            kwargs['yes_price'] = price_cents

        response = await asyncio.to_thread(
            lambda: self._portfolio_api.create_order(**kwargs)
        )

        if hasattr(response, 'to_dict'):
            return response.to_dict()
        return response if isinstance(response, dict) else {'order': response}

    async def _get_order_status(self, order_id: str) -> dict[str, Any]:
        """Get order details from Kalshi API."""
        response = await asyncio.to_thread(
            lambda: self._portfolio_api.get_order(order_id)
        )
        if hasattr(response, 'to_dict'):
            return response.to_dict()
        return response if isinstance(response, dict) else {}

    async def _process_order_response(
        self,
        response: dict[str, Any],
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> Order:
        """Process Kalshi order response into internal Order object."""
        order_data = response.get('order', response)
        order_id = order_data.get('order_id', '')
        logger.info('Kalshi order data: %s', order_data)

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

        # Use data from create_order response directly; fall back to
        # get_order only when needed fields are missing.
        order_detail = order_data
        remaining_count = int(order_detail.get('remaining_count', 0))
        total_count = int(order_detail.get('count', int(quantity)))
        api_status = order_detail.get('status', '')

        # For resting/pending orders, try to get fill info from API
        if api_status in ('resting', 'pending') and remaining_count > 0:
            try:
                order_details = await self._get_order_status(order_id)
                order_detail = order_details.get('order', order_details)
                remaining_count = int(
                    order_detail.get('remaining_count', remaining_count)
                )
                total_count = int(order_detail.get('count', total_count))
            except Exception:
                logger.debug(
                    'Could not fetch order status for %s, using create response',
                    order_id,
                )

        filled_count = total_count - remaining_count

        if filled_count == 0 and api_status not in ('resting', 'pending'):
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

        filled_quantity = Decimal(str(filled_count))
        filled_price = limit_price
        remaining = Decimal(str(remaining_count))
        commission = filled_quantity * filled_price * self.commission_rate

        trades = []
        if filled_count > 0:
            trades.append(
                Trade(
                    side=side,
                    ticker=ticker,
                    price=filled_price,
                    quantity=filled_quantity,
                    commission=commission,
                )
            )

        if remaining_count == 0:
            order_status = OrderStatus.FILLED
        elif filled_count > 0:
            order_status = OrderStatus.PARTIALLY_FILLED
        else:
            # Resting/pending — accepted but not yet filled
            order_status = OrderStatus.PLACED

        return Order(
            status=order_status,
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            filled_quantity=filled_quantity,
            average_price=filled_price,
            trades=trades,
            remaining=remaining,
            commission=commission,
        )

    async def _alert_rejected(self, reason: OrderFailureReason, ticker: Ticker) -> None:
        if self.alerter:
            try:
                await self.alerter.on_order_rejected(reason, ticker)
            except Exception:
                pass

    async def place_order(  # noqa: C901
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

        # Validate inputs
        if quantity <= 0 or limit_price <= 0:
            await self._alert_rejected(OrderFailureReason.INVALID_ORDER, ticker)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.INVALID_ORDER,
            )

        if not isinstance(ticker, KalshiTicker) or not ticker.market_ticker:
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

        # Check cash
        if side == TradeSide.BUY:
            cash_position = self.position_manager.get_position(ticker.collateral)
            cash_required = (
                quantity * limit_price * (Decimal('1') + self.commission_rate)
            )
            if cash_position is None or cash_position.quantity < cash_required:
                logger.warning(
                    'Insufficient cash for %s: need %s, have %s',
                    ticker.symbol,
                    cash_required,
                    cash_position.quantity if cash_position else 0,
                )
                await self._alert_rejected(OrderFailureReason.INSUFFICIENT_CASH, ticker)
                return PlaceOrderResult(
                    order=None,
                    failure_reason=OrderFailureReason.INSUFFICIENT_CASH,
                )

        # Risk check
        if not await self.risk_manager.check_trade(ticker, side, quantity, limit_price):
            await self._alert_rejected(OrderFailureReason.RISK_CHECK_FAILED, ticker)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.RISK_CHECK_FAILED,
            )

        try:
            # Convert internal price (0.01-0.99) to Kalshi cents (1-99)
            price_cents = int(limit_price * 100)
            count = int(quantity)

            action = 'buy' if side == TradeSide.BUY else 'sell'

            # Determine Kalshi API side based on ticker
            kalshi_side = ticker.side if isinstance(ticker, KalshiTicker) else 'yes'

            response = await self._submit_order(
                action=action,
                side=kalshi_side,
                ticker=ticker,
                price_cents=price_cents,
                count=count,
            )

            order = await self._process_order_response(
                response, side, ticker, limit_price, quantity
            )

            # Update positions
            for trade in order.trades:
                self.position_manager.apply_trade(trade)

            self.orders.append(order)
            if order.status == OrderStatus.REJECTED:
                failure_reason = OrderFailureReason.UNKNOWN
                await self._alert_rejected(failure_reason, ticker)
                return PlaceOrderResult(order=order, failure_reason=failure_reason)
            return PlaceOrderResult(order=order)

        except Exception as e:
            logger.exception('Error placing Kalshi order for %s: %s', ticker.symbol, e)
            return PlaceOrderResult(
                order=None,
                failure_reason=OrderFailureReason.UNKNOWN,
            )
