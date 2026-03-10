"""HubDataSource — DataSource that reads events from a running MarketDataHub.

Drop-in replacement for LivePolyMarketDataSource / LiveKalshiDataSource / CompositeDataSource.
Connects to the hub Unix socket and reconstructs Event objects from the JSON stream.
Reconnects automatically with exponential backoff if the hub is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from coinjure.data.source import DataSource
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.ticker import KalshiTicker, PolyMarketTicker

logger = logging.getLogger(__name__)


class HubDataSource(DataSource):
    """DataSource that reads events from a running MarketDataHub via Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        queue_size: int = 1000,
        tickers: list[str] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._reader_task: asyncio.Task | None = None
        self._running: bool = False
        self._tickers: set[str] = set(tickers) if tickers else set()
        self._sub_id: int | None = None

    async def start(self) -> None:
        self._running = True
        self._reader_task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    def watch_token(self, token_id: str) -> None:
        """Add token to local filter and relay to hub."""
        self._tickers.add(token_id)
        payload: dict[str, Any] = {'cmd': 'watch_token', 'token_id': token_id}
        if self._sub_id is not None:
            payload['sub_id'] = self._sub_id
        self._send_control(payload)

    def unwatch_token(self, token_id: str) -> None:
        """Remove token from local filter and relay to hub."""
        self._tickers.discard(token_id)
        payload: dict[str, Any] = {'cmd': 'unwatch_token', 'token_id': token_id}
        if self._sub_id is not None:
            payload['sub_id'] = self._sub_id
        self._send_control(payload)

    def _send_control(self, payload: dict) -> None:
        """Fire-and-forget: send a control command to the hub via a new connection."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_send_control(payload))
        except RuntimeError:
            # No running loop — ignore silently
            pass

    async def _async_send_control(self, payload: dict) -> None:
        """Send a single control command to the hub and discard the response."""
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            writer.write((json.dumps(payload) + '\n').encode())
            await writer.drain()
            # Read response (hub expects it)
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        except Exception:
            logger.debug(
                'HubDataSource: control command failed: %s',
                payload.get('cmd'),
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        """Connect to hub with exponential backoff on failure."""
        backoff = 1.0
        while self._running:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path)
                )
                logger.info('HubDataSource: connected to %s', self.socket_path)
                backoff = 1.0
                await self._read_events(reader, writer)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                if self._running:
                    logger.warning(
                        'HubDataSource: cannot connect to hub (%s), retry in %.0fs',
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('HubDataSource: connection error', exc_info=True)
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def _read_events(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # Send subscribe message with current ticker filter
            subscribe_msg = (
                json.dumps(
                    {
                        'cmd': 'subscribe',
                        'tickers': list(self._tickers),
                    }
                )
                + '\n'
            )
            writer.write(subscribe_msg.encode())
            await writer.drain()

            # Read ack
            ack_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if ack_line:
                try:
                    ack = json.loads(ack_line.decode())
                    self._sub_id = ack.get('sub_id')
                except json.JSONDecodeError:
                    pass

            # Stream events
            while self._running:
                line = await reader.readline()
                if not line:
                    logger.info('HubDataSource: hub closed connection, will reconnect')
                    break
                event = self._deserialize(line.decode())
                if event is not None:
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        self._queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
        finally:
            self._sub_id = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _deserialize(self, line: str) -> Event | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = data.get('type')
        ticker_type = data.get('ticker_type', 'unknown')
        ticker_data = data.get('ticker', {})

        if ticker_type == 'polymarket':
            ticker = PolyMarketTicker(
                symbol=ticker_data.get('symbol', ''),
                name=ticker_data.get('name', ''),
                token_id=ticker_data.get('token_id', ''),
                market_id=ticker_data.get('market_id', ''),
                event_id=ticker_data.get('event_id', ''),
                side=ticker_data.get('side', 'yes'),
            )
        elif ticker_type == 'kalshi':
            ticker = KalshiTicker(
                symbol=ticker_data.get('symbol', ''),
                name=ticker_data.get('name', ''),
                market_ticker=ticker_data.get('market_ticker', ''),
                event_ticker=ticker_data.get('event_ticker', ''),
                series_ticker=ticker_data.get('series_ticker', ''),
                side=ticker_data.get('side', 'yes'),
            )
        else:
            return None

        if event_type == 'OrderBookEvent':
            try:
                return OrderBookEvent(
                    ticker=ticker,
                    price=Decimal(data['price']),
                    size=Decimal(data['size']),
                    size_delta=Decimal(data['size_delta']),
                    side=data.get('side', ''),
                )
            except Exception:
                return None

        if event_type == 'PriceChangeEvent':
            try:
                ts_str = data.get('timestamp')
                ts = datetime.fromisoformat(ts_str) if ts_str else None
                return PriceChangeEvent(
                    ticker=ticker,
                    price=Decimal(data['price']),
                    timestamp=ts,
                )
            except Exception:
                return None

        return None
