import json
import logging
from decimal import Decimal

from coinjure.events.events import Event, PriceChangeEvent
from coinjure.ticker.ticker import PolyMarketTicker

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
            with open(self.history_file) as f:
                for line in f:
                    data = json.loads(line.strip())
                    if (
                        data.get('event_id') == self.ticker.event_id
                        and data.get('market_id') == self.ticker.market_id
                    ):
                        ts = data.get('time_series')
                        ts_yes = ts.get('Yes')
                        if ts_yes:
                            for entry in ts_yes:
                                timestamp = entry.get('t')
                                price = entry.get('p')
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
        events.sort(key=lambda e: e.timestamp)
        logger.info('Historical data loaded: %d events', len(events))
        return events

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None
