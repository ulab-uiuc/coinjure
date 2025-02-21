from abc import ABC, abstractmethod
from decimal import Decimal

from ticker.ticker import Ticker
from trader.types import TradeSide


class RiskManager(ABC):
    @abstractmethod
    async def check_trade(
        self, ticker: Ticker, side: TradeSide, quantity: Decimal, price: Decimal
    ) -> bool:
        """Check if a trade meets risk management criteria."""
        pass


class NoRiskManager(RiskManager):
    async def check_trade(
        self, ticker: Ticker, side: TradeSide, quantity: Decimal, price: Decimal
    ) -> bool:
        return True
