from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from coinjure.events import Event

logger = logging.getLogger(__name__)


class DataSource(ABC):
    @abstractmethod
    async def get_next_event(self) -> Event | None:
        """Retrieve next event. Returns ``None`` if finished (or on timeout)."""
        pass

    async def start(self) -> None:  # noqa: B027
        """Lifecycle hook — called once before the engine begins polling.

        Override in subclasses that need to launch background polling tasks
        (e.g. ``LivePolyMarketDataSource``).  The default implementation is
        a no-op so that simple / historical data sources need not override.
        """

    async def stop(self) -> None:  # noqa: B027
        """Lifecycle hook — called when the engine shuts down.

        Override to cancel background tasks or close connections.
        """


class CompositeDataSource(DataSource):
    """Merges events from multiple DataSource instances into a single stream.

    Each child data source runs its own polling loop. Events from all sources
    are funneled into a shared queue and delivered to the engine in arrival order.
    """

    def __init__(self, sources: list[DataSource]) -> None:
        self.sources = sources
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=2000)
        self._relay_tasks: list[asyncio.Task] = []

    async def _relay(self, source: DataSource) -> None:
        """Continuously relay events from one child source into the shared queue."""
        while True:
            try:
                event = await source.get_next_event()
                if event is not None:
                    await self._queue.put(event)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    'Relay error from %s', type(source).__name__, exc_info=True
                )
                await asyncio.sleep(1.0)

    async def start(self) -> None:
        for source in self.sources:
            await source.start()
        for source in self.sources:
            task = asyncio.create_task(self._relay(source))
            self._relay_tasks.append(task)

    async def stop(self) -> None:
        for task in self._relay_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._relay_tasks.clear()
        for source in self.sources:
            try:
                await source.stop()
            except Exception:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    def drain_pending_events(self) -> list[object]:
        """Drain all pending events from the queue (non-blocking)."""
        events: list[object] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except Exception:
                break
        return events

    def register_token_ticker(self, token_id: str, ticker: object) -> None:
        for source in self.sources:
            reg = getattr(source, 'register_token_ticker', None)
            if reg:
                reg(token_id, ticker)

    def watch_token(self, token_id: str) -> None:
        for source in self.sources:
            watch = getattr(source, 'watch_token', None)
            if watch:
                watch(token_id)

    def unwatch_token(self, token_id: str) -> None:
        for source in self.sources:
            unwatch = getattr(source, 'unwatch_token', None)
            if unwatch:
                unwatch(token_id)


def build_market_source(
    exchange: str,
) -> CompositeDataSource | DataSource:
    """Build a market data source for the given exchange.

    - polymarket      -> CompositeDataSource([poly, rss_news])
    - kalshi          -> CompositeDataSource([kalshi, rss_news])
    - cross_platform  -> CompositeDataSource([poly, kalshi, rss_news])

    Raises :exc:`ValueError` for unsupported exchange values.
    """
    from coinjure.data.live.kalshi import LiveKalshiDataSource
    from coinjure.data.live.polymarket import (
        LivePolyMarketDataSource,
        LiveRSSNewsDataSource,
    )

    rss = LiveRSSNewsDataSource()

    if exchange == 'polymarket':
        poly = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=120.0,
            orderbook_refresh_interval=5.0,
            reprocess_on_start=False,
        )
        return CompositeDataSource([poly, rss])
    if exchange == 'kalshi':
        kalshi = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        return CompositeDataSource([kalshi, rss])
    if exchange == 'cross_platform':
        poly = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        kalshi = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        return CompositeDataSource([poly, kalshi, rss])
    raise ValueError(f'Unsupported exchange: {exchange!r}')
