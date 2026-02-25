from abc import ABC, abstractmethod

from swm_agent.events.events import Event
from swm_agent.trader.trader import Trader


class Strategy(ABC):
    def set_paused(self, paused: bool) -> None:
        """Set control-plane pause state for this strategy."""
        setattr(self, '_paused', paused)

    def is_paused(self) -> bool:
        """Return whether control-plane has paused decision-making."""
        return bool(getattr(self, '_paused', False))

    @abstractmethod
    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process an event"""
        pass
