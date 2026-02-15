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

    @property
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
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

    @property
    def collateral(self) -> Ticker:
        """The ticker of the collateral currency"""
        raise NotImplementedError('Cash tickers do not have a collateral ticker')


CashTicker.POLYMARKET_USDC = CashTicker(
    symbol='PolyMarket_USDC', name='PolyMarket USDC'
)
