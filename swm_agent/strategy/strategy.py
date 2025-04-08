from abc import ABC, abstractmethod

from swm_agent.events.events import Event
from swm_agent.trader.trader import Trader


class Strategy(ABC):
    @abstractmethod
    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process an event"""
        pass
