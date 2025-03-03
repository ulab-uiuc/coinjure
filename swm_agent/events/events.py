from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from ..ticker.ticker import PolyMarketTicker


class Event(ABC):
    @abstractmethod
    def trigger(self) -> None:
        pass


class OrderBookEvent(Event):
    ticker: PolyMarketTicker
    price: Decimal
    size: Decimal
    size_delta: Decimal

    def __init__(
        self,
        ticker: PolyMarketTicker,
        price: Decimal,
        size: Decimal,
        size_delta: Decimal,
    ):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.size_delta = size_delta

    def trigger(self) -> None:
        pass


class NewsEvent(Event):
    news: str
    title: str
    source: str
    url: str
    published_at: Optional[datetime]
    categories: List[str]
    description: str
    image_url: str
    uuid: str
    event_id: str
    ticker: PolyMarketTicker

    def __init__(
        self,
        news: str,
        title: str = '',
        source: str = '',
        url: str = '',
        published_at: Optional[datetime] = None,
        categories: List[str] = None,
        description: str = '',
        image_url: str = '',
        uuid: str = '',
        event_id: str = '',
        ticker: PolyMarketTicker = None,
    ):
        self.news = news
        self.title = title
        self.source = source
        self.url = url
        self.published_at = published_at or datetime.now()
        self.categories = categories or []
        self.description = description
        self.image_url = image_url
        self.uuid = uuid
        self.event_id = event_id
        self.ticker = ticker

    def trigger(self) -> None:
        pass
