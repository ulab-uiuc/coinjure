from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Ticker(ABC):
    @property
    @abstractmethod
    def symbol(self) -> str:
        """The symbol of the ticker, must be unique across all markets"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """The name of the ticker"""
        pass

    @property
    @abstractmethod
    def collateral(self) -> 'Ticker':
        """The ticker of the collateral currency"""
        pass


@dataclass
class PolyMarketTicker(Ticker):
    def __init__(
        self,
        symbol: str,
        name: str = '',
        token_id: str = '',
        market_id: str = '',
        event_id: str = '',
    ):
        self._symbol = symbol
        self._name = name
        self._token_id = token_id
        self._market_id = market_id
        self._event_id = event_id

    @property
    def symbol(self) -> str:
        """The symbol of the ticker, must be unique across all markets"""
        return self._symbol

    @property
    def name(self) -> str:
        """The name of the ticker"""
        return self._name

    @property
    def collateral(self) -> 'Ticker':
        """The ticker of the collateral currency"""
        return CashTicker.POLYMARKET_USDC

    @property
    def token_id(self) -> str:
        """PolyMarket token ID used for order book and price history"""
        return self._token_id

    @property
    def market_id(self) -> str:
        """PolyMarket market ID"""
        return self._market_id

    @property
    def event_id(self) -> str:
        """PolyMarket event ID the market belongs to"""
        return self._event_id

    @classmethod
    def from_token_id(cls, token_id: str, name: str = '') -> 'PolyMarketTicker':
        """Create a ticker from a token ID, using the token ID as the symbol"""
        return cls(symbol=token_id, name=name, token_id=token_id)


@dataclass
class CashTicker(Ticker):
    def __init__(self, symbol: str, name: str):
        self._symbol = symbol
        self._name = name

    @property
    def symbol(self) -> str:
        """The symbol of the ticker, must be unique across all markets"""
        return self._symbol

    @property
    def name(self) -> str:
        """The name of the cash ticker"""
        return self._name

    @property
    def collateral(self) -> 'Ticker':
        """The ticker of the collateral currency"""
        raise NotImplementedError('Cash tickers do not have a collateral ticker')


CashTicker.POLYMARKET_USDC = CashTicker(
    symbol='PolyMarket_USDC', name='PolyMarket USDC'
)
