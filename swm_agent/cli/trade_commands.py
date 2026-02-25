"""Engine control CLI commands (pause/resume/status/stop)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from swm_agent.cli.control import SOCKET_PATH, run_command


def _print_response(resp: dict, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(resp))
        return
    if resp.get('ok'):
        status = resp.get('status') or resp.get('error') or 'ok'
        click.echo(status)
    else:
        click.echo(f"error: {resp.get('error', 'unknown')}")


def _resolve_socket(socket: str | None) -> Path:
    return Path(socket) if socket else SOCKET_PATH


@click.group()
def trade() -> None:
    """Engine control commands for operators and agents."""


@trade.command('pause')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Control socket path'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def pause_cmd(socket: str | None, as_json: bool) -> None:
    """Pause decision-making (data ingestion continues)."""
    sock = _resolve_socket(socket)
    resp = run_command('pause', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@trade.command('resume')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Control socket path'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def resume_cmd(socket: str | None, as_json: bool) -> None:
    """Resume decision-making."""
    sock = _resolve_socket(socket)
    resp = run_command('resume', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@trade.command('status')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Control socket path'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def status_cmd(socket: str | None, as_json: bool) -> None:
    """Show engine runtime status."""
    sock = _resolve_socket(socket)
    resp = run_command('status', socket_path=sock)
    if as_json:
        click.echo(json.dumps(resp))
    else:
        if not resp.get('ok'):
            click.echo(f"error: {resp.get('error', 'unknown')}")
            raise SystemExit(1)
        click.echo(
            'status={status} paused={paused} runtime={runtime} events={events} '
            'decisions={decisions} executed={executed} orders={orders}'.format(
                status='ok',
                paused=resp.get('paused', False),
                runtime=resp.get('runtime', '0:00:00'),
                events=resp.get('event_count', 0),
                decisions=resp.get('decisions', 0),
                executed=resp.get('executed', 0),
                orders=resp.get('orders', 0),
            )
        )


@trade.command('stop')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Control socket path'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def stop_cmd(socket: str | None, as_json: bool) -> None:
    """Gracefully stop the engine."""
    sock = _resolve_socket(socket)
    resp = run_command('stop', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@trade.command('estop')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Control socket path'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def estop_cmd(socket: str | None, as_json: bool) -> None:
    """Emergency stop alias (sends the same stop signal immediately)."""
    sock = _resolve_socket(socket)
    resp = run_command('stop', socket_path=sock)
    if as_json:
        click.echo(json.dumps(resp))
    else:
        if resp.get('ok'):
            click.echo('EMERGENCY STOP signal sent')
        else:
            click.echo(f"error: {resp.get('error', 'unknown')}")
            raise SystemExit(1)
