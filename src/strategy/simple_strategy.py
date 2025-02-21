from events.events import Event, NewsEvent, OrderBookEvent
from strategy.strategy import Strategy
from trader.trader import Trader


class SimpleStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader):
        if isinstance(event, OrderBookEvent):
            print(f"OrderBookEvent: {event}")
        elif isinstance(event, NewsEvent):
            print(f"NewsEvent: {event}")
