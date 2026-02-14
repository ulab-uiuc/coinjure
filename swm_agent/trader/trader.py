from abc import ABC, abstractmethod
from decimal import Decimal
from typing import List

from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import PositionManager
from swm_agent.risk.risk_manager import RiskManager
from swm_agent.ticker.ticker import Ticker
from swm_agent.trader.types import Order, PlaceOrderResult, TradeSide


class Trader(ABC):
    def __init__(
        self,
        market_data: MarketDataManager,
        risk_manager: RiskManager,
        position_manager: PositionManager,
    ):
        self.market_data = market_data
        self.risk_manager = risk_manager
        self.position_manager = position_manager
        self.orders: List[Order] = []

    @abstractmethod
    async def place_order(
        self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal
    ) -> PlaceOrderResult:
        """Place an order."""
        pass
