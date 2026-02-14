from abc import ABC, abstractmethod
from typing import Optional

from swm_agent.events.events import Event


class DataSource(ABC):
    @abstractmethod
    async def get_next_event(self) -> Optional[Event]:
        """Retrieve next event. Returns ``None`` if finished (or on timeout)."""
        pass

    async def start(self) -> None:
        """Lifecycle hook — called once before the engine begins polling.

        Override in subclasses that need to launch background polling tasks
        (e.g. ``LivePolyMarketDataSource``).  The default implementation is
        a no-op so that simple / historical data sources need not override.
        """

    async def stop(self) -> None:
        """Lifecycle hook — called when the engine shuts down.

        Override to cancel background tasks or close connections.
        """
