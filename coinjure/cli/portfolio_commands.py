"""Portfolio supervisor CLI — manage the multi-strategy portfolio lifecycle.

Commands
--------
  portfolio list          — show all strategies and their status
  portfolio add           — register a new strategy (pending_backtest)
  portfolio promote       — advance lifecycle: pending_backtest → paper_trading → live_trading
  portfolio retire        — stop and archive a strategy
  portfolio health-check  — detect stale, degraded, or dead strategies
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from coinjure.cli.control import SOCKET_DIR, run_command
from coinjure.data.hub.hub import HUB_SOCKET_PATH
from coinjure.portfolio.registry import REGISTRY_PATH, StrategyEntry, StrategyRegistry

# ── Helpers ────────────────────────────────────────────────────────────────────


def _load_registry() -> StrategyRegistry:
    return StrategyRegistry(REGISTRY_PATH)


def _emit_json(data: Any) -> None:
    click.echo(json.dumps(data, default=str))


def _coinjure_cmd() -> str:
    """Return the path to the `coinjure` CLI binary (or a fallback invocation)."""
    found = shutil.which('coinjure')
    return found or sys.executable + ' -m coinjure.cli.cli'


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is currently running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _socket_status(socket_path: str) -> dict:
    """Query the control socket; return the status dict or an error dict."""
    try:
        return run_command('status', socket_path=Path(socket_path))
    except FileNotFoundError:
        return {'ok': False, 'error': 'socket_not_found'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


async def _gather_socket_statuses(
    entries: list[StrategyEntry],
) -> list[tuple[StrategyEntry, dict]]:
    """Query all active-strategy sockets in parallel."""

    async def _query(entry: StrategyEntry) -> tuple[StrategyEntry, dict]:
        if not entry.socket_path:
            return entry, {'ok': False, 'error': 'no_socket_path'}
        return entry, await asyncio.to_thread(_socket_status, entry.socket_path)

    active = [e for e in entries if e.lifecycle in ('paper_trading', 'live_trading')]
    if not active:
        return []
    results = await asyncio.gather(*[_query(e) for e in active])
    return list(results)


# ── Click group ────────────────────────────────────────────────────────────────


@click.group()
def portfolio() -> None:
    """Portfolio supervisor — manage multiple parallel strategy instances."""


# ── list ───────────────────────────────────────────────────────────────────────


@portfolio.command('list')
@click.option('--lifecycle', default=None, help='Filter by lifecycle stage.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def portfolio_list(lifecycle: str | None, as_json: bool) -> None:
    """Show all strategies in the portfolio registry."""
    reg = _load_registry()
    entries = reg.list()
    if lifecycle:
        entries = [e for e in entries if e.lifecycle == lifecycle]

    if as_json:
        _emit_json(
            {
                'ok': True,
                'count': len(entries),
                'strategies': [e.to_dict() for e in entries],
            }
        )
        return

    if not entries:
        click.echo('Portfolio is empty.')
        return

    click.echo(
        f'Portfolio — {len(entries)} strateg{"y" if len(entries)==1 else "ies"}:\n'
    )
    for e in entries:
        status_marker = {
            'pending_backtest': '[BACKTEST]',
            'paper_trading': '[PAPER]   ',
            'live_trading': '[LIVE]    ',
            'retired': '[RETIRED] ',
        }.get(e.lifecycle, '[?]       ')
        pid_info = f' pid={e.pid}' if e.pid else ''
        pnl_info = f' pnl={e.paper_pnl}' if e.paper_pnl else ''
        click.echo(f'{status_marker} {e.strategy_id}{pid_info}{pnl_info}')
        click.echo(f'           ref: {e.strategy_ref}')
        if e.retired_reason:
            click.echo(f'           reason: {e.retired_reason}')
    click.echo()


# ── add ────────────────────────────────────────────────────────────────────────


@portfolio.command('add')
@click.option(
    '--strategy-id', required=True, help='Unique identifier for this strategy instance.'
)
@click.option(
    '--strategy-ref',
    required=True,
    help='Strategy ref: module:Class or /path/file.py:Class',
)
@click.option(
    '--kwargs-json', default='{}', help='JSON object of strategy constructor kwargs.'
)
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'cross_platform']),
    default='cross_platform',
    show_default=True,
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def portfolio_add(
    strategy_id: str,
    strategy_ref: str,
    kwargs_json: str,
    exchange: str,
    as_json: bool,
) -> None:
    """Register a new strategy in the portfolio (lifecycle: pending_backtest)."""
    try:
        kwargs = json.loads(kwargs_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f'Invalid --kwargs-json: {exc.msg}') from exc
    if not isinstance(kwargs, dict):
        raise click.ClickException('--kwargs-json must be a JSON object.')

    reg = _load_registry()
    data_dir = str(Path('data') / 'research' / strategy_id)
    entry = StrategyEntry(
        strategy_id=strategy_id,
        strategy_ref=strategy_ref,
        strategy_kwargs=kwargs,
        lifecycle='pending_backtest',
        exchange=exchange,
        data_dir=data_dir,
    )
    try:
        reg.add(entry)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    resp = {'ok': True, 'strategy_id': strategy_id, 'lifecycle': 'pending_backtest'}
    if as_json:
        _emit_json(resp)
    else:
        click.echo(f'Registered {strategy_id!r} (pending_backtest).')


# ── promote ────────────────────────────────────────────────────────────────────


_LIFECYCLE_ORDER = ['pending_backtest', 'paper_trading', 'live_trading']

_LIFECYCLE_NEXT: dict[str, str] = {
    'pending_backtest': 'paper_trading',
    'paper_trading': 'live_trading',
}


@portfolio.command('promote')
@click.option('--strategy-id', required=True)
@click.option(
    '--to',
    'target_lifecycle',
    required=True,
    type=click.Choice(['paper_trading', 'live_trading']),
    help='Target lifecycle stage.',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--hub-socket',
    default=None,
    type=click.Path(),
    help='Override hub socket path (default: auto-detect ~/.coinjure/hub.sock).',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def portfolio_promote(
    strategy_id: str,
    target_lifecycle: str,
    initial_capital: str,
    hub_socket: str | None,
    as_json: bool,
) -> None:
    """Promote a strategy to the next lifecycle stage and launch its process."""
    reg = _load_registry()
    entry = reg.get(strategy_id)
    if entry is None:
        raise click.ClickException(f'Strategy not found: {strategy_id!r}')

    if entry.lifecycle == 'retired':
        raise click.ClickException(f'Cannot promote a retired strategy.')

    expected_next = _LIFECYCLE_NEXT.get(entry.lifecycle)
    if expected_next != target_lifecycle:
        raise click.ClickException(
            f'Cannot promote from {entry.lifecycle!r} to {target_lifecycle!r}. '
            f'Expected next stage: {expected_next!r}.'
        )

    # Build socket path for this strategy instance.
    socket_path = SOCKET_DIR / f'{strategy_id}.sock'

    # Auto-detect hub: use explicit --hub-socket, or fall back to default path if running.
    resolved_hub_socket: str | None = None
    if hub_socket:
        resolved_hub_socket = str(Path(hub_socket).expanduser())
    elif HUB_SOCKET_PATH.exists():
        resolved_hub_socket = str(HUB_SOCKET_PATH)
        if not as_json:
            click.echo(f'Using shared Market Data Hub: {HUB_SOCKET_PATH}')

    # Build subprocess command.
    coinjure = shutil.which('coinjure')
    if not coinjure:
        raise click.ClickException(
            'Could not find `coinjure` binary. Activate the poetry virtualenv first.'
        )

    if target_lifecycle == 'paper_trading':
        cmd = [
            coinjure,
            'paper',
            'run',
            '--exchange',
            entry.exchange,
            '--strategy-ref',
            entry.strategy_ref,
            '--initial-capital',
            initial_capital,
            '--socket-path',
            str(socket_path),
        ]
        if entry.strategy_kwargs:
            cmd += ['--strategy-kwargs-json', json.dumps(entry.strategy_kwargs)]
        if resolved_hub_socket:
            cmd += ['--hub-socket', resolved_hub_socket]

    else:  # live_trading
        cmd = [
            coinjure,
            'live',
            'run',
            '--exchange',
            entry.exchange,
            '--strategy-ref',
            entry.strategy_ref,
            '--socket-path',
            str(socket_path),
        ]
        if entry.strategy_kwargs:
            cmd += ['--strategy-kwargs-json', json.dumps(entry.strategy_kwargs)]

    # Launch detached subprocess.
    log_dir = Path('data') / 'research' / strategy_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'process.log'

    try:
        with open(log_file, 'a') as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,  # detach from current session
            )
    except Exception as exc:
        raise click.ClickException(f'Failed to launch subprocess: {exc}') from exc

    # Wait up to 5 seconds for socket to appear.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if socket_path.exists():
            break
        time.sleep(0.3)

    # Update registry.
    entry.lifecycle = target_lifecycle
    entry.pid = proc.pid
    entry.socket_path = str(socket_path)
    reg.update(entry)

    resp: dict = {
        'ok': True,
        'strategy_id': strategy_id,
        'lifecycle': target_lifecycle,
        'pid': proc.pid,
        'socket': str(socket_path),
        'log': str(log_file),
        'hub_socket': resolved_hub_socket,
    }
    if as_json:
        _emit_json(resp)
    else:
        click.echo(
            f'Promoted {strategy_id!r} → {target_lifecycle}  pid={proc.pid}  '
            f'socket={socket_path}'
        )


# ── retire ─────────────────────────────────────────────────────────────────────


@portfolio.command('retire')
@click.option('--strategy-id', required=True)
@click.option('--reason', default='manual', show_default=True)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def portfolio_retire(strategy_id: str, reason: str, as_json: bool) -> None:
    """Stop a running strategy and mark it as retired."""
    reg = _load_registry()
    entry = reg.get(strategy_id)
    if entry is None:
        raise click.ClickException(f'Strategy not found: {strategy_id!r}')

    stop_result: dict = {'ok': True, 'status': 'not_running'}

    # Auto-harvest performance before stopping.
    if entry.socket_path and Path(entry.socket_path).exists():
        try:
            status_resp = run_command('status', socket_path=Path(entry.socket_path))
            if status_resp.get('ok'):
                from coinjure.research.ledger import FeedbackEntry, FeedbackLedger

                fb = FeedbackEntry(
                    strategy_id=strategy_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source='paper' if entry.lifecycle == 'paper_trading' else 'live',
                    runtime_seconds=status_resp.get('runtime', 0),
                    metrics={
                        'realized_pnl': status_resp.get('portfolio', {}).get(
                            'realized_pnl'
                        ),
                        'event_count': status_resp.get('event_count', 0),
                        'total_orders': status_resp.get('orders', 0),
                    },
                    decision_stats=status_resp.get('decision_stats', {}),
                    notes=f'auto-harvested on retire: {reason}',
                )
                FeedbackLedger().append(fb)
        except Exception:  # noqa: BLE001
            pass  # best-effort harvest

    # Send stop command if socket exists.
    if entry.socket_path and Path(entry.socket_path).exists():
        try:
            stop_result = run_command('stop', socket_path=Path(entry.socket_path))
        except Exception as exc:
            stop_result = {'ok': False, 'error': str(exc)}

    entry.lifecycle = 'retired'
    entry.retired_reason = reason
    entry.pid = None
    entry.socket_path = None
    reg.update(entry)

    resp = {
        'ok': True,
        'strategy_id': strategy_id,
        'lifecycle': 'retired',
        'reason': reason,
        'stop_result': stop_result,
    }
    if as_json:
        _emit_json(resp)
    else:
        click.echo(f'Retired {strategy_id!r}: {reason}')


# ── health-check ───────────────────────────────────────────────────────────────

_STALE_DAYS = 7
_NO_SIGNAL_HOURS = 24


@portfolio.command('health-check')
@click.option(
    '--update/--no-update',
    default=True,
    show_default=True,
    help='Write back PnL / last_signal_at to registry after querying sockets.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def portfolio_health_check(update: bool, as_json: bool) -> None:
    """Detect stale, degraded, or dead strategy instances."""
    reg = _load_registry()
    entries = reg.list()

    # Query all active sockets in parallel.
    socket_results: dict[str, dict] = {}
    active_entries = [
        e for e in entries if e.lifecycle in ('paper_trading', 'live_trading')
    ]
    if active_entries:
        results = asyncio.run(_gather_socket_statuses(entries))
        for e, status in results:
            socket_results[e.strategy_id] = status

    now = datetime.now(tz=timezone.utc)
    stale: list[dict] = []
    degraded: list[dict] = []
    dead_process: list[dict] = []
    healthy: list[dict] = []

    for entry in entries:
        if entry.lifecycle not in ('paper_trading', 'live_trading'):
            continue

        sid = entry.strategy_id
        entry_info = {'id': sid, 'lifecycle': entry.lifecycle}

        # 1. Check if OS process is alive.
        if entry.pid is not None and not _is_pid_alive(entry.pid):
            dead_process.append(
                {**entry_info, 'reason': 'pid_not_found', 'pid': entry.pid}
            )
            continue

        # 2. Check socket reachability.
        status = socket_results.get(sid, {})
        if not status.get('ok'):
            dead_process.append(
                {
                    **entry_info,
                    'reason': 'socket_unreachable',
                    'error': status.get('error'),
                }
            )
            continue

        # 3. Update registry with live PnL if requested.
        if update:
            portfolio_val = (status.get('portfolio') or {}).get('total')
            if portfolio_val is not None:
                entry.paper_pnl = str(round(float(portfolio_val) - 10000, 2))
            last_activity = status.get('last_activity') or ''
            if last_activity:
                entry.last_signal_at = last_activity
            reg.update(entry)

        # 4. Stale: no signal for > STALE_DAYS days.
        if entry.last_signal_at:
            try:
                last_ts = datetime.fromisoformat(entry.last_signal_at)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                delta_days = (now - last_ts).total_seconds() / 86400
                if delta_days > _STALE_DAYS:
                    stale.append(
                        {
                            **entry_info,
                            'reason': 'no_signal_7d',
                            'days': round(delta_days, 1),
                        }
                    )
                    continue
            except ValueError:
                pass

        # 5. No signal at all but has been running > NO_SIGNAL_HOURS.
        if entry.last_signal_at is None:
            try:
                created_ts = datetime.fromisoformat(entry.created_at)
                if created_ts.tzinfo is None:
                    created_ts = created_ts.replace(tzinfo=timezone.utc)
                hours_running = (now - created_ts).total_seconds() / 3600
                if hours_running > _NO_SIGNAL_HOURS:
                    stale.append(
                        {
                            **entry_info,
                            'reason': 'never_signaled',
                            'hours': round(hours_running, 1),
                        }
                    )
                    continue
            except ValueError:
                pass

        # 6. Degraded: consecutive losses from decision stats.
        decision_stats = status.get('decision_stats') or {}
        consecutive_losses = decision_stats.get('consecutive_losses', 0)
        if consecutive_losses and int(consecutive_losses) >= 10:
            degraded.append(
                {
                    **entry_info,
                    'reason': 'consecutive_loss',
                    'count': consecutive_losses,
                }
            )
            continue

        healthy.append({**entry_info, 'paper_pnl': entry.paper_pnl})

    report = {
        'ok': True,
        'stale': stale,
        'degraded': degraded,
        'dead_process': dead_process,
        'healthy': healthy,
        'summary': {
            'total_active': len(active_entries),
            'healthy': len(healthy),
            'issues': len(stale) + len(degraded) + len(dead_process),
        },
    }

    if as_json:
        _emit_json(report)
        return

    def _section(label: str, items: list[dict]) -> None:
        if not items:
            return
        click.echo(f'\n{label} ({len(items)}):')
        for item in items:
            click.echo(f'  - {item["id"]}: {item.get("reason", "")}')

    _section('Dead processes', dead_process)
    _section('Stale (no recent signal)', stale)
    _section('Degraded (consecutive losses)', degraded)
    _section('Healthy', healthy)

    summary = report['summary']
    click.echo(
        f'\nSummary: {summary["total_active"]} active, '
        f'{summary["healthy"]} healthy, '
        f'{summary["issues"]} issues\n'
    )
