from abc import ABC, abstractmethod
from decimal import Decimal

from ticker.ticker import Ticker


class Event(ABC):
    @abstractmethod
    def trigger(self) -> None:
        pass


class OrderBookEvent(Event):
    def __init__(
        self, ticker: Ticker, price: Decimal, size: Decimal, size_delta: Decimal
    ):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.size_delta = size_delta


class NewsEvent(Event):
    def __init__(self, news: str):
        self.news = news
