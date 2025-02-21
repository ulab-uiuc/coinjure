from events.events import OrderBookEvent
from order.order_book import Level, OrderBook
from ticker.ticker import Ticker


class MarketDataManager:
    def __init__(self) -> None:
        self.order_books: dict[Ticker, OrderBook] = {}

    def update_order_book(self, ticker: Ticker, order_book: OrderBook) -> None:
        self.order_books[ticker] = order_book

    def process_orderbook_event(self, event: OrderBookEvent) -> None:
        """Update order book"""
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

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
