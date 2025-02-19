from abc import ABC, abstractmethod
from decimal import Decimal
from ticker.ticker import Ticker
from trader.types import TradeSide
from data.market_data_manager import MarketDataManager
from risk.risk_manager import RiskManager
from position.position_manager import PositionManager

class Trader(ABC):
    def __init__(self,
                 market_data: MarketDataManager,
                 risk_manager: RiskManager,
                 position_manager: PositionManager):
        self.market_data = market_data
        self.risk_manager = risk_manager
        self.position_manager = position_manager
    
    @abstractmethod
    async def place_order(self, side: TradeSide, ticker: Ticker, limit_price: Decimal, quantity: Decimal):
        """Place an order."""
        pass