from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from coinjure.ticker import Ticker

if TYPE_CHECKING:
    pass


class Event(ABC):
    """Base class for all market events.

    Attributes:
        timestamp_epoch: Unix timestamp (seconds since epoch) for when the event
            was created.  Defaults to ``time.time()`` at construction.
        dedup_id: Short unique identifier (12-char UUID prefix) useful for
            deduplicating events across restarts.  Named ``dedup_id`` rather
            than ``event_id`` to avoid colliding with the Polymarket-specific
            ``event_id`` that already exists on several concrete event classes.
    """

    timestamp_epoch: float
    dedup_id: str

    def __init__(self) -> None:
        if not hasattr(self, 'timestamp_epoch'):
            self.timestamp_epoch = time.time()
        if not hasattr(self, 'dedup_id'):
            self.dedup_id = str(uuid4())[:12]

    @abstractmethod
    def trigger(self) -> None:
        pass

    @abstractmethod
    def __str__(self) -> str:
        pass

    @abstractmethod
    def __repr__(self) -> str:
        pass


class OrderBookEvent(Event):
    ticker: Ticker
    price: Decimal
    size: Decimal
    size_delta: Decimal
    side: str  # 'bid' or 'ask'

    def __init__(
        self,
        ticker: Ticker,
        price: Decimal,
        size: Decimal,
        size_delta: Decimal,
        side: str = '',
    ):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.size_delta = size_delta
        self.side = side
        super().__init__()

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        return f'OrderBookEvent: ticker={self.ticker.symbol}, price={self.price}, size={self.size}, size_delta={self.size_delta}'

    def __repr__(self) -> str:
        return self.__str__()


class NewsEvent(Event):
    news: str
    title: str
    source: str
    url: str
    published_at: datetime | None
    categories: list[str]
    description: str
    image_url: str
    uuid: str
    event_id: str
    ticker: Ticker

    def __init__(
        self,
        news: str,
        title: str = '',
        source: str = '',
        url: str = '',
        published_at: datetime | None = None,
        categories: list[str] = None,
        description: str = '',
        image_url: str = '',
        uuid: str = '',
        event_id: str = '',
        ticker: Ticker = None,
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
        super().__init__()

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        ticker_str = f'{self.ticker.symbol}' if self.ticker else 'None'
        content = f'{self.news[:100]}{"..." if len(self.news) > 100 else ""}'
        return f'NewsEvent: ticker={ticker_str}, title={self.title}, source={self.source}\n  Content: {content}'

    def __repr__(self) -> str:
        return self.__str__()


class PriceChangeEvent(Event):
    def __init__(
        self,
        ticker: Ticker,
        price: Decimal,
        timestamp: datetime = None,
    ):
        self.ticker = ticker
        self.price = price
        self.timestamp = timestamp or datetime.now()
        super().__init__()

    def trigger(self) -> None:
        pass

    def __str__(self) -> str:
        return f'PriceChangeEvent: ticker={self.ticker.symbol}, price={self.price}, timestamp={self.timestamp}'

    def __repr__(self) -> str:
        return self.__str__()


def build_mock_events(ticker: Ticker, n_events: int) -> list[Event]:
    """Generate a list of synthetic PriceChangeEvent / OrderBookEvent for dry runs."""
    prices = [
        Decimal('0.47'),
        Decimal('0.49'),
        Decimal('0.46'),
        Decimal('0.51'),
        Decimal('0.48'),
    ]
    events: list[Event] = []
    for i in range(max(1, n_events)):
        base = prices[i % len(prices)]
        if i % 2 == 0:
            events.append(
                PriceChangeEvent(
                    ticker=ticker,
                    price=base,
                    timestamp=None,
                )
            )
            continue

        side = 'bid' if i % 4 == 1 else 'ask'
        price = base - Decimal('0.01') if side == 'bid' else base + Decimal('0.01')
        size = Decimal('100') + Decimal(i * 10)
        events.append(
            OrderBookEvent(
                ticker=ticker,
                price=price,
                size=size,
                size_delta=Decimal('10'),
                side=side,
            )
        )
    return events
