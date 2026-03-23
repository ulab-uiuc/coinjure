from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from coinjure.data.manager import DataManager
from coinjure.ticker import CashTicker, KalshiTicker, Ticker
from coinjure.trading.position import Position, PositionManager
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

        # Store credentials for raw HTTP calls (SDK omits fields like prices/positions)
        self._key_id = key_id
        self._private_key = self._load_private_key(pk_path)

        self._sync_usd_balance()
        self._sync_positions()

    @staticmethod
    def _load_private_key(pk_path: str) -> Any:
        try:
            from cryptography.hazmat.primitives import serialization
            with open(pk_path, 'rb') as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        except Exception:
            return None

    def _raw_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Authenticated raw HTTP GET — bypasses SDK which omits many response fields."""
        import requests
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = ts + 'GET' + path.split('?')[0]
        sig_b64 = ''
        if self._private_key is not None:
            sig = self._private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
            sig_b64 = base64.b64encode(sig).decode()
        headers = {
            'KALSHI-ACCESS-KEY': self._key_id or '',
            'KALSHI-ACCESS-TIMESTAMP': ts,
            'KALSHI-ACCESS-SIGNATURE': sig_b64,
        }
        r = requests.get(
            f'https://api.elections.kalshi.com{path}',
            params=params,
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def _sync_usd_balance(self) -> None:
        """Re-fetch USD balance from Kalshi and update position manager."""
        try:
            balance_response = self._portfolio_api.get_balance()
            actual_balance = Decimal(str(balance_response.balance)) / Decimal('100')
            existing = self.position_manager.get_position(CashTicker.KALSHI_USD)
            self.position_manager.update_position(
                Position(
                    ticker=CashTicker.KALSHI_USD,
                    quantity=actual_balance,
                    average_cost=Decimal('1'),
                    realized_pnl=existing.realized_pnl if existing else Decimal('0'),
                )
            )
        except Exception:
            pass  # Non-critical — local tracking continues as fallback

    def _sync_positions(self) -> None:
        """Load existing Kalshi market positions into position_manager at startup.

        Uses raw HTTP because the SDK's get_positions() returns None for all fields.
        Positive quantity = holding YES contracts; negative = holding NO contracts.
        """
        try:
            data = self._raw_get('/trade-api/v2/portfolio/positions')
            market_positions = data.get('market_positions') or []
            count = 0
            for pos in market_positions:
                market_id: str = pos.get('market_id', '')
                quantity: int = pos.get('position', 0)  # +YES / -NO
                if not market_id or quantity == 0:
                    continue
                # Determine side: positive = YES contracts, negative = NO contracts
                if quantity > 0:
                    side = 'yes'
                    qty = Decimal(str(quantity))
                else:
                    side = 'no'
                    qty = Decimal(str(-quantity))

                # Symbol format must match LiveKalshiDataSource:
                # YES → symbol=market_id, NO → symbol=f'{market_id}_NO'
                symbol = market_id if side == 'yes' else f'{market_id}_NO'
                ticker = KalshiTicker(
                    symbol=symbol,
                    market_ticker=market_id,
                    side=side,
                )
                # Use market_exposure (cents) as cost basis if available
                exposure_cents = pos.get('market_exposure', 0) or 0
                avg_cost = (
                    Decimal(str(exposure_cents)) / Decimal('100') / qty
                    if qty > 0 else Decimal('0')
                )
                self.position_manager.update_position(
                    Position(
                        ticker=ticker,
                        quantity=qty,
                        average_cost=avg_cost,
                        realized_pnl=Decimal('0'),
                    )
                )
                count += 1
                logger.info(
                    'Loaded existing position: %s %s contracts @ avg $%s',
                    market_id, qty, avg_cost,
                )
            if count:
                logger.info('Synced %d existing Kalshi positions at startup', count)
        except Exception:
            logger.debug('_sync_positions() failed — starting with empty positions', exc_info=True)

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
        api_status = order_detail.get('status', '')

        # SDK to_dict() often returns None for count/remaining_count.
        # 'executed' status means fully filled regardless of count fields.
        # For other statuses where count is None, treat as unfilled (resting).
        _count_raw = order_detail.get('count')
        _remaining_raw = order_detail.get('remaining_count')
        if api_status == 'executed':
            remaining_count = 0
            total_count = int(quantity)
        elif _count_raw is None or _remaining_raw is None:
            # Unknown fill state — treat conservatively as unfilled
            remaining_count = int(quantity)
            total_count = int(quantity)
        else:
            remaining_count = int(_remaining_raw)
            total_count = int(_count_raw)

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
            order_id=order_id,
        )

    async def _alert_rejected(self, reason: OrderFailureReason, ticker: Ticker) -> None:
        if self.alerter:
            try:
                await self.alerter.on_order_rejected(reason, ticker)
            except Exception:
                pass

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting Kalshi order."""
        try:
            await asyncio.to_thread(
                lambda: self._portfolio_api.cancel_order(order_id)
            )
            logger.info('Cancelled Kalshi order %s', order_id)
            return True
        except Exception:
            logger.warning('Failed to cancel Kalshi order %s', order_id, exc_info=True)
            return False

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
            self._sync_usd_balance()

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
