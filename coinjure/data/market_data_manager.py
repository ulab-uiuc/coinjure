from decimal import Decimal

from coinjure.events.events import OrderBookEvent, PriceChangeEvent
from coinjure.order.order_book import Level, OrderBook
from coinjure.ticker.ticker import Ticker


class MarketDataManager:
    def __init__(self) -> None:
        self.order_books: dict[Ticker, OrderBook] = {}

    def update_order_book(self, ticker: Ticker, order_book: OrderBook) -> None:
        self.order_books[ticker] = order_book

    def process_orderbook_event(self, event: OrderBookEvent) -> None:
        """Update order book from an incremental OrderBookEvent.

        Each event carries a single price level with its current size and side.
        We merge it into the existing order book, keeping levels sorted.
        """
        if event.ticker not in self.order_books:
            self.order_books[event.ticker] = OrderBook()

        ob = self.order_books[event.ticker]
        level = Level(price=event.price, size=event.size)

        if event.side == 'bid':
            self._upsert_level(ob.bids, level, descending=True)
        elif event.side == 'ask':
            self._upsert_level(ob.asks, level, descending=False)

    @staticmethod
    def _upsert_level(levels: list[Level], new: Level, descending: bool) -> None:
        """Insert or update a price level in a sorted list."""
        for i, existing in enumerate(levels):
            if existing.price == new.price:
                if new.size <= 0:
                    levels.pop(i)
                else:
                    levels[i] = new
                return
        if new.size > 0:
            levels.append(new)
            levels.sort(key=lambda lv: lv.price, reverse=descending)

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

        # Bootstrap No-side order book on first encounter so PaperTrader
        # can fill No orders before the first No PriceChangeEvent arrives.
        # Subsequent updates come from the No events emitted by the data source.
        no_ticker = getattr(event.ticker, 'get_no_ticker', lambda: None)()
        if no_ticker is not None and no_ticker not in self.order_books:
            self.order_books[no_ticker] = OrderBook()

            no_price = Decimal('1') - event.price
            no_bid = max(Decimal('0'), no_price - half_spread)
            no_ask = min(Decimal('1'), no_price + half_spread)

            no_bids = [Level(price=no_bid, size=size)] if no_bid > Decimal('0') else []
            no_asks = [Level(price=no_ask, size=size)] if no_ask < Decimal('1') else []

            no_ob = self.order_books[no_ticker]
            no_ob.update(asks=no_asks, bids=no_bids)

    def remove_ticker(self, ticker: Ticker) -> None:
        """Remove all order book data for a ticker."""
        self.order_books.pop(ticker, None)

    def prune_stale_tickers(self, active_symbols: set[str]) -> int:
        """Remove order books for tickers whose symbol is not in *active_symbols*.

        Returns the number of tickers removed.
        """
        stale = [t for t in self.order_books if t.symbol not in active_symbols]
        for t in stale:
            del self.order_books[t]
        return len(stale)

    def get_bids(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        ob = self.order_books.get(ticker)
        return ob.get_bids(depth) if ob is not None else []

    def get_asks(self, ticker: Ticker, depth: int | None = None) -> list[Level]:
        ob = self.order_books.get(ticker)
        return ob.get_asks(depth) if ob is not None else []

    def get_best_bid(self, ticker: Ticker) -> Level | None:
        ob = self.order_books.get(ticker)
        return ob.best_bid if ob is not None else None

    def get_best_ask(self, ticker: Ticker) -> Level | None:
        ob = self.order_books.get(ticker)
        return ob.best_ask if ob is not None else None
