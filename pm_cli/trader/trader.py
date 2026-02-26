from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pm_cli.data.market_data_manager import MarketDataManager
from pm_cli.position.position_manager import PositionManager
from pm_cli.risk.risk_manager import RiskManager
from pm_cli.ticker.ticker import Ticker
from pm_cli.trader.types import (
    Order,
    OrderFailureReason,
    PlaceOrderResult,
    TradeSide,
)

if TYPE_CHECKING:
    from pm_cli.alerts.alerter import Alerter


class Trader(ABC):
    def __init__(
        self,
        market_data: MarketDataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
        alerter: Alerter | None = None,
    ):
        self.market_data = market_data
        self.risk_manager = risk_manager
        self.position_manager = position_manager
        self.alerter = alerter
        self.orders: list[Order] = []
        self.read_only: bool = False
        self._seen_client_order_ids: set[str] = set()
        self._seen_client_order_queue: deque[str] = deque()
        self._max_seen_client_order_ids: int = 5000

    @abstractmethod
    async def place_order(
        self,
        side: TradeSide,
        ticker: Ticker,
        limit_price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> PlaceOrderResult:
        """Place an order."""
        pass

    def set_read_only(self, enabled: bool) -> None:
        """Enable/disable read-only mode (blocks new orders when enabled)."""
        self.read_only = enabled

    def _kill_switch_active(self) -> bool:
        """Global kill-switch that can be toggled outside the process.

        Either:
        - PRED_MARKET_CLI_KILL_SWITCH=1
        - PRED_MARKET_CLI_KILL_SWITCH_FILE points to a file that exists
        """
        if os.environ.get('PRED_MARKET_CLI_KILL_SWITCH', '').strip() == '1':
            return True
        kill_file = os.environ.get('PRED_MARKET_CLI_KILL_SWITCH_FILE', '').strip()
        if kill_file:
            return os.path.exists(kill_file)
        default_kill_file = Path.home() / '.pm-cli' / 'kill.switch'
        return default_kill_file.exists()

    def _check_order_guard(
        self, client_order_id: str | None
    ) -> OrderFailureReason | None:
        """Validate global trade guards and idempotency keys."""
        if self.read_only or self._kill_switch_active():
            return OrderFailureReason.TRADING_DISABLED
        if client_order_id:
            if client_order_id in self._seen_client_order_ids:
                return OrderFailureReason.DUPLICATE_ORDER
            self._seen_client_order_ids.add(client_order_id)
            self._seen_client_order_queue.append(client_order_id)
            while len(self._seen_client_order_queue) > self._max_seen_client_order_ids:
                stale = self._seen_client_order_queue.popleft()
                self._seen_client_order_ids.discard(stale)
        return None
