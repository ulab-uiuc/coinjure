import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from coinjure.events.events import Event, PriceChangeEvent
from coinjure.ticker.ticker import PolyMarketTicker

from .history_reader import iter_history_rows
from ..data_source import DataSource

logger = logging.getLogger(__name__)


class HistoricalDataSource(DataSource):
    def __init__(self, history_file: str, ticker: PolyMarketTicker):
        self.history_file = history_file
        self.ticker = ticker
        self.events = self._load_events()
        self.index = 0

    def _load_events(self) -> list[Event]:
        events: list[Event] = []
        no_ticker = self.ticker.get_no_ticker()

        try:
            for data in iter_history_rows(self.history_file):
                if (
                    str(data.get('event_id')) == self.ticker.event_id
                    and str(data.get('market_id')) == self.ticker.market_id
                ):
                    ts = data.get('time_series')
                    ts_yes = ts.get('Yes') if isinstance(ts, dict) else None
                    if ts_yes:
                        for entry in ts_yes:
                            if not isinstance(entry, dict):
                                continue
                            timestamp = entry.get('t')
                            price = entry.get('p')
                            if price is None:
                                continue
                            yes_price = Decimal(str(price))
                            event = PriceChangeEvent(
                                ticker=self.ticker,
                                price=yes_price,
                                timestamp=timestamp,
                            )
                            events.append(event)

                            # Also emit No-side price event
                            if no_ticker is not None:
                                no_price = Decimal('1') - yes_price
                                no_event = PriceChangeEvent(
                                    ticker=no_ticker,
                                    price=no_price,
                                    timestamp=timestamp,
                                )
                                events.append(no_event)
        except Exception as e:
            logger.error('Error loading events from %s: %s', self.history_file, e)

        # Sort events by timestamp
        events.sort(key=lambda e: self._timestamp_sort_key(e.timestamp))
        logger.info('Historical data loaded: %d events', len(events))
        return events

    @staticmethod
    def _timestamp_sort_key(value: Any) -> float:
        if isinstance(value, bool):
            return float('inf')
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return float('inf')
            try:
                return float(raw)
            except ValueError:
                pass
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return float('inf')
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return float('inf')

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None
