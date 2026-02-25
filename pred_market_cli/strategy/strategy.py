from abc import ABC, abstractmethod

from pred_market_cli.events.events import Event
from pred_market_cli.trader.trader import Trader


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
