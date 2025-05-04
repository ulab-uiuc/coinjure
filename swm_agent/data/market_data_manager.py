from decimal import Decimal

from swm_agent.events.events import OrderBookEvent, PriceChangeEvent
from swm_agent.order.order_book import Level, OrderBook
from swm_agent.ticker.ticker import Ticker


class MarketDataManager:
    def __init__(self) -> None:
        self.order_books: dict[Ticker, OrderBook] = {}

    def update_order_book(self, ticker: Ticker, order_book: OrderBook) -> None:
        self.order_books[ticker] = order_book

    def process_orderbook_event(self, event: OrderBookEvent) -> None:
        """Update order book"""
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

    def process_price_change_event(self, event: PriceChangeEvent) -> None:
        """Update order book based on probability change event"""
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

        spread: Decimal = Decimal('0.01')  # Default spread of 0.01
        half_spread = spread / Decimal('2')
        bid_price = max(Decimal('0'), event.price - half_spread)
        ask_price = min(Decimal('1'), event.price + half_spread)

        size = Decimal('1000')

        bids = [Level(price=bid_price, size=size)] if bid_price > Decimal('0') else []
        asks = [Level(price=ask_price, size=size)] if ask_price < Decimal('1') else []

        order_book = self.order_books[event.ticker]
        order_book.update(asks=asks, bids=bids)

    def get_bids(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        return (
            self.order_books[ticker].get_bids(depth)
            if ticker in self.order_books
            else []
        )

    def get_asks(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        return (
            self.order_books[ticker].get_asks(depth)
            if ticker in self.order_books
            else []
        )

    def get_best_bid(self, ticker: Ticker) -> Level:
        return self.order_books[ticker].best_bid

    def get_best_ask(self, ticker: Ticker) -> Level:
        return self.order_books[ticker].best_ask
