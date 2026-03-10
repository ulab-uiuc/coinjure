from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(eq=True, frozen=True)
class Ticker(ABC):
    # The symbol of the ticker, must be unique across all markets
    symbol: str = field(metadata={'abstract': True})
    # The name of the ticker
    name: str = field(metadata={'abstract': True})
    # YES or NO side of the market
    side: str = 'yes'

    @property
    def identifier(self) -> str:
        """Canonical market-level identifier for matching across strategies."""
        return self.symbol

    @property
    @abstractmethod
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
        pass


@dataclass(eq=True, frozen=True)
class PolyMarketTicker(Ticker):
    symbol: str
    name: str = ''
    token_id: str = ''
    market_id: str = ''
    event_id: str = ''
    side: str = 'yes'

    @property
    def identifier(self) -> str:
        return self.market_id or self.token_id or self.symbol

    @property
    def collateral(self) -> Ticker:
        return CashTicker.POLYMARKET_USDC

    @classmethod
    def from_token_id(cls, token_id: str, name: str = '') -> PolyMarketTicker:
        """Create a ticker from a token ID, using the token ID as the symbol"""
        return cls(symbol=token_id, name=name, token_id=token_id)


@dataclass(eq=True, frozen=True)
class CashTicker(Ticker):
    symbol: str
    name: str

    POLYMARKET_USDC: ClassVar[CashTicker]
    KALSHI_USD: ClassVar[CashTicker]

    @property
    def collateral(self) -> Ticker:
        raise NotImplementedError('Cash tickers do not have a collateral ticker')


CashTicker.POLYMARKET_USDC = CashTicker(
    symbol='PolyMarket_USDC', name='PolyMarket USDC'
)


@dataclass(eq=True, frozen=True)
class KalshiTicker(Ticker):
    symbol: str
    name: str = ''
    market_ticker: str = ''
    event_ticker: str = ''
    series_ticker: str = ''
    side: str = 'yes'

    @property
    def identifier(self) -> str:
        return self.market_ticker or self.symbol

    @property
    def market_id(self) -> str:
        """Alias for market_ticker, unified interface with PolyMarketTicker."""
        return self.market_ticker

    @property
    def event_id(self) -> str:
        """Alias for event_ticker, unified interface with PolyMarketTicker."""
        return self.event_ticker

    @property
    def collateral(self) -> Ticker:
        return CashTicker.KALSHI_USD


CashTicker.KALSHI_USD = CashTicker(symbol='Kalshi_USD', name='Kalshi USD')
