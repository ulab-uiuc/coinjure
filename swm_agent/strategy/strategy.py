from abc import ABC, abstractmethod

from events.events import Event
from trader.trader import Trader


class Strategy(ABC):
    @abstractmethod
    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process an event"""
        pass
