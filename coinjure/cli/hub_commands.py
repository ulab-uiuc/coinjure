"""Hub CLI — start/stop/status for the shared Market Data Hub process."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import click

from coinjure.cli.utils import _emit
from coinjure.market.hub.hub import HUB_PID_PATH, HUB_SOCKET_PATH

_DEFAULT_SOCKET = str(HUB_SOCKET_PATH)


@click.group()
def hub() -> None:
    """Market Data Hub — shared exchange poller for all strategy processes."""


# ── start ──────────────────────────────────────────────────────────────────────


@hub.command('start')
@click.option(
    '--socket-path', default=_DEFAULT_SOCKET, show_default=True, type=click.Path()
)
@click.option(
    '--poly-interval',
    default=60.0,
    show_default=True,
    type=float,
    help='Polymarket polling interval in seconds.',
)
@click.option(
    '--kalshi-interval',
    default=60.0,
    show_default=True,
    type=float,
    help='Kalshi polling interval in seconds.',
)
@click.option('--kalshi-api-key-id', default=None, help='Kalshi API key id.')
@click.option(
    '--kalshi-private-key-path', default=None, help='Kalshi private key path.'
)
@click.option(
    '--detach/--no-detach',
    default=False,
    help='Run as a detached background process (default: foreground).',
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON output.'
)
def hub_start(
    socket_path: str,
    poly_interval: float,
    kalshi_interval: float,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    detach: bool,
    as_json: bool,
) -> None:
    """Start the Market Data Hub (shared exchange poller)."""
    socket = Path(socket_path).expanduser()

    if detach:
        coinjure_bin = shutil.which('coinjure')
        if coinjure_bin:
            cmd = [coinjure_bin, 'hub', 'start']
        else:
            cmd = [sys.executable, '-m', 'coinjure.cli.cli', 'hub', 'start']

        cmd += [
            '--socket-path',
            str(socket),
            '--poly-interval',
            str(poly_interval),
            '--kalshi-interval',
            str(kalshi_interval),
            '--no-detach',
        ]
        if kalshi_api_key_id:
            cmd += ['--kalshi-api-key-id', kalshi_api_key_id]
        if kalshi_private_key_path:
            cmd += ['--kalshi-private-key-path', kalshi_private_key_path]

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        HUB_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        HUB_PID_PATH.write_text(str(proc.pid))

        _emit({'ok': True, 'pid': proc.pid, 'socket': str(socket)}, as_json=as_json)
        return

    # Foreground mode — build composite source and run hub
    from coinjure.market.composite_data_source import CompositeDataSource
    from coinjure.market.hub.hub import MarketDataHub
    from coinjure.market.live.kalshi_data_source import LiveKalshiDataSource
    from coinjure.market.live.live_data_source import LivePolyMarketDataSource

    poly = LivePolyMarketDataSource(
        event_cache_file='events_cache.jsonl',
        polling_interval=poly_interval,
        orderbook_refresh_interval=10.0,
        reprocess_on_start=False,
    )
    kalshi = LiveKalshiDataSource(
        api_key_id=kalshi_api_key_id,
        private_key_path=kalshi_private_key_path,
        event_cache_file='kalshi_events_cache.jsonl',
        polling_interval=kalshi_interval,
        reprocess_on_start=False,
    )
    source = CompositeDataSource([poly, kalshi])
    hub_obj = MarketDataHub(socket_path=socket, source=source)

    if not as_json:
        click.echo(f'Starting Market Data Hub on {socket}  (Ctrl-C to stop)')
    try:
        asyncio.run(hub_obj.start())
    except KeyboardInterrupt:
        if not as_json:
            click.echo('\nHub stopped.')


# ── status ─────────────────────────────────────────────────────────────────────


@hub.command('status')
@click.option(
    '--socket-path', default=_DEFAULT_SOCKET, show_default=True, type=click.Path()
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def hub_status(socket_path: str, as_json: bool) -> None:
    """Show hub status: subscriber count, uptime, total events forwarded."""
    socket = Path(socket_path).expanduser()
    if not socket.exists():
        _emit(
            {'ok': False, 'error': f'Hub not running (socket not found: {socket})'},
            as_json=as_json,
        )
        return

    result = _send_hub_command(socket, 'status')
    _emit(result, as_json=as_json)


# ── stop ───────────────────────────────────────────────────────────────────────


@hub.command('stop')
@click.option(
    '--socket-path', default=_DEFAULT_SOCKET, show_default=True, type=click.Path()
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def hub_stop(socket_path: str, as_json: bool) -> None:
    """Stop the running Market Data Hub."""
    socket = Path(socket_path).expanduser()
    if not socket.exists():
        _emit(
            {'ok': False, 'error': f'Hub not running (socket not found: {socket})'},
            as_json=as_json,
        )
        return

    result = _send_hub_command(socket, 'stop')
    _emit(result, as_json=as_json)


# ── Internal helper ────────────────────────────────────────────────────────────


def _send_hub_command(socket: Path, cmd: str) -> dict:
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
