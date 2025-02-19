from typing import List
from data.data_source import DataSource
from events.events import Event

class HistoricalDataSource(DataSource):
    def __init__(self, history_file: str):
        self.history_file = history_file
        self.events = self._load_events()
        self.index = 0

    def _load_events(self) -> List[Event]:
        events = []
        return events

    async def get_next_event(self):
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None