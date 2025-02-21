from events.events import Event, NewsEvent, OrderBookEvent
from trader.trader import Trader

from .strategy import Strategy


class SimpleStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        if isinstance(event, OrderBookEvent):
            print(f'OrderBookEvent: {event}')
        elif isinstance(event, NewsEvent):
            print(f'NewsEvent: {event}')
