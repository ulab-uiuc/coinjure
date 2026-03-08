"""MarketDataHub — single-process exchange data fan-out server.

Runs a Unix socket server that accepts two kinds of connections:
  - Subscribers  (no initial data within 0.1 s): receive a stream of newline-delimited
                  JSON event lines until they disconnect.
  - Control clients (send JSON within 0.1 s):    get a single JSON response, then close.

Supported control commands:
  {"cmd": "status"}   → {"ok": true, "subscribers": N, "events_total": M, "uptime_s": T}
  {"cmd": "stop"}     → {"ok": true, "status": "stopping"}  (then hub shuts down)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from coinjure.data.source import DataSource
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.ticker import KalshiTicker, PolyMarketTicker

logger = logging.getLogger(__name__)

SOCKET_DIR = Path.home() / '.coinjure'
HUB_SOCKET_PATH = SOCKET_DIR / 'hub.sock'
HUB_PID_PATH = SOCKET_DIR / 'hub.pid'


class MarketDataHub:
    """Single-process exchange data fan-out server."""

    def __init__(self, socket_path: Path, source: DataSource) -> None:
        self.socket_path = socket_path
        self._source = source
        # Per-subscriber queues: id → queue of encoded bytes
        self._subscribers: dict[int, asyncio.Queue[bytes]] = {}
        self._sub_tasks: set[asyncio.Task] = set()
        self._next_id = 0
        self._server: asyncio.AbstractServer | None = None
        self._fan_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._start_time: float = 0.0
        self._events_total: int = 0
        self._running: bool = False

    async def start(self) -> None:
        """Start the hub and block until stop() is called or a stop command is received."""
        self._stop_event = asyncio.Event()
        self._start_time = time.monotonic()
        self._running = True

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        await self._source.start()

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        logger.info('MarketDataHub listening on %s', self.socket_path)

        self._fan_task = asyncio.create_task(self._fan_loop())

        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            # Cancel subscriber connection tasks
            for task in list(self._sub_tasks):
                task.cancel()
            if self._sub_tasks:
                await asyncio.gather(*self._sub_tasks, return_exceptions=True)
            # Cancel fan loop
            if self._fan_task:
                self._fan_task.cancel()
                try:
                    await self._fan_task
                except asyncio.CancelledError:
                    pass
            # Stop server
            if self._server:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:
                    pass
            # Stop source
            try:
                await self._source.stop()
            except Exception:
                pass
            # Remove socket file
            try:
                self.socket_path.unlink(missing_ok=True)
            except Exception:
                pass
            logger.info('MarketDataHub stopped.')

    async def stop(self) -> None:
        """Signal the hub to stop. Returns immediately; cleanup happens in start()."""
        self._running = False
        if self._stop_event:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # Fan-out loop
    # ------------------------------------------------------------------

    async def _fan_loop(self) -> None:
        """Pull events from source and broadcast to all subscriber queues."""
        while self._running:
            try:
                event = await self._source.get_next_event()
                if event is None:
                    continue
                if not isinstance(event, (OrderBookEvent, PriceChangeEvent)):
                    continue
                line = self._serialize_event(event)
                if line is None:
                    continue
                self._events_total += 1
                encoded = (line + '\n').encode()
                dead: list[int] = []
                for sub_id, q in list(self._subscribers.items()):
                    if q.full():
                        try:
                            q.get_nowait()  # drop oldest to make room
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(encoded)
                    except Exception:
                        dead.append(sub_id)
                for sub_id in dead:
                    self._subscribers.pop(sub_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('Hub fan loop error', exc_info=True)
                await asyncio.sleep(1.0)

    def _serialize_event(self, event: Event) -> str | None:
        ticker = getattr(event, 'ticker', None)
        if ticker is None:
            return None

        if isinstance(ticker, PolyMarketTicker):
            ticker_data: dict[str, Any] = {
                'symbol': ticker.symbol,
                'name': ticker.name,
                'token_id': ticker.token_id,
                'market_id': ticker.market_id,
                'event_id': ticker.event_id,
                'side': ticker.side,
            }
            ticker_type = 'polymarket'
        elif isinstance(ticker, KalshiTicker):
            ticker_data = {
                'symbol': ticker.symbol,
                'name': ticker.name,
                'market_ticker': ticker.market_ticker,
                'event_ticker': ticker.event_ticker,
                'series_ticker': ticker.series_ticker,
                'side': ticker.side,
            }
            ticker_type = 'kalshi'
        else:
            return None  # skip unknown ticker types

        payload: dict[str, Any] = {'ticker_type': ticker_type, 'ticker': ticker_data}

        if isinstance(event, OrderBookEvent):
            payload['type'] = 'OrderBookEvent'
            payload['price'] = str(event.price)
            payload['size'] = str(event.size)
            payload['size_delta'] = str(event.size_delta)
            payload['side'] = event.side
        elif isinstance(event, PriceChangeEvent):
            payload['type'] = 'PriceChangeEvent'
            payload['price'] = str(event.price)
            payload['timestamp'] = event.timestamp.isoformat()
        else:
            return None

        return json.dumps(payload, separators=(',', ':'))

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._sub_tasks.add(task)
        try:
            # Detect connection type: control client vs. subscriber
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                raw = b''

            if raw.strip():
                await self._handle_control(raw, writer)
            else:
                await self._handle_subscriber(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug('Hub connection error', exc_info=True)
        finally:
            if task:
                self._sub_tasks.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_subscriber(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sub_id = self._next_id
        self._next_id += 1
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._subscribers[sub_id] = q
        logger.debug(
            'Hub: subscriber %d connected (total=%d)', sub_id, len(self._subscribers)
        )
        try:
            while self._running:
                data = await q.get()
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.pop(sub_id, None)
            logger.debug(
                'Hub: subscriber %d disconnected (total=%d)',
                sub_id,
                len(self._subscribers),
            )

    async def _handle_control(self, raw: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(raw.decode())
            cmd = req.get('cmd', '')
            if cmd == 'status':
                uptime = time.monotonic() - self._start_time
                resp: dict[str, Any] = {
                    'ok': True,
                    'subscribers': len(self._subscribers),
                    'events_total': self._events_total,
                    'uptime_s': round(uptime, 1),
                }
            elif cmd == 'stop':
                resp = {'ok': True, 'status': 'stopping'}
                writer.write((json.dumps(resp) + '\n').encode())
                await writer.drain()
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                # Schedule stop so we can still send the response first
                asyncio.get_event_loop().call_soon(
                    lambda: asyncio.ensure_future(self.stop())
                )
                return
            else:
                resp = {'ok': False, 'error': f'Unknown command: {cmd!r}'}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            resp = {'ok': False, 'error': str(exc)}

        try:
            writer.write((json.dumps(resp) + '\n').encode())
            await writer.drain()
        except Exception:
            pass


def send_hub_command(socket: Path, cmd: str) -> dict:
    """Send a JSON control command to the hub and return the parsed response."""

    async def _query() -> dict:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        payload = (json.dumps({'cmd': cmd}) + '\n').encode()
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            result = json.loads(raw.decode())
        except asyncio.TimeoutError:
            result = {'ok': False, 'error': 'timeout waiting for hub response'}
        except json.JSONDecodeError as exc:
            result = {'ok': False, 'error': f'invalid response: {exc}'}
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return result

    try:
        return asyncio.run(_query())
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
