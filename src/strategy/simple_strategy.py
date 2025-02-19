from decimal import Decimal
from events.events import OrderBookEvent, NewsEvent, Event
from strategy.strategy import Strategy
from trader.trader import Trader
from trader.types import TradeSide
from ticker.tickers import PolymarketTicker

class SimpleStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader):
        if isinstance(event, OrderBookEvent):
            print(f"OrderBookEvent: {event}")
        elif isinstance(event, NewsEvent):
            print(f"NewsEvent: {event}") 