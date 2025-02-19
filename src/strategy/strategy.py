from abc import ABC, abstractmethod
from trader.trader import Trader
from events.events import Event

class Strategy(ABC):
    @abstractmethod
    async def process_event(self, event: Event, trader: Trader):
        """Process an event"""
        pass 