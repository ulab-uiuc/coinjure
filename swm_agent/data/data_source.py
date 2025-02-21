from abc import ABC, abstractmethod


class DataSource(ABC):
    @abstractmethod
    async def get_next_event(self) -> None:
        """Retrieves next event. Returns None if finished."""
        pass
