"""Engine CLI group — unified paper/live trading, instance management, and batch ops.

Merges the old paper, live, trade, portfolio, and monitor groups into a single
noun-first ``engine`` group.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from coinjure.cli.utils import _emit
from coinjure.data.source import build_market_source as _domain_build_market_source
from coinjure.engine.control import SOCKET_DIR, SOCKET_PATH, run_command
from coinjure.engine.registry import REGISTRY_PATH, StrategyEntry, StrategyRegistry
from coinjure.hub.hub import HUB_SOCKET_PATH
from coinjure.strategy.loader import load_strategy as _domain_load_strategy
from coinjure.strategy.strategy import IdleStrategy

# ── CLI helpers (formerly in agent_commands.py) ───────────────────────────────


def _parse_strategy_kwargs_json(strategy_kwargs_json: str | None) -> dict[str, Any]:
    if not strategy_kwargs_json:
        return {}
    try:
        parsed = json.loads(strategy_kwargs_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f'Invalid --strategy-kwargs-json: {exc.msg}'
        ) from exc
    if not isinstance(parsed, dict):
        raise click.ClickException('--strategy-kwargs-json must be a JSON object.')
    return parsed


def _confirm_live_trading(*, as_json: bool) -> None:
    """Require explicit user confirmation before starting live trading."""
    disclaimer = (
        'DISCLAIMER: Live trading places real orders with real funds. '
        'You are fully responsible for all losses, fees, and operational risk.'
    )
    if as_json:
        raise click.ClickException(
            'Live trading confirmation required in interactive mode.'
        )
    click.echo(click.style(disclaimer, fg='yellow'))
    confirmed = click.confirm(
        'Proceed with live trading?',
        default=True,
        show_default=True,
    )
    if not confirmed:
        raise click.ClickException('Live trading cancelled by user.')


def _build_market_source(exchange: str):
    """Build a market data source, raising ClickException on error."""
    try:
        return _domain_build_market_source(exchange)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _load_strategy(strategy_ref: str, strategy_kwargs: dict[str, Any] | None = None):
    """Load and instantiate a strategy, raising ClickException on error."""
    try:
        return _domain_load_strategy(strategy_ref, strategy_kwargs)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


# ── Helpers ────────────────────────────────────────────────────────────────────


def _load_registry() -> StrategyRegistry:
    return StrategyRegistry(REGISTRY_PATH)


def _emit_json(data: Any) -> None:
    click.echo(json.dumps(data, default=str))


def _coinjure_cmd() -> str:
    found = shutil.which('coinjure')
    return found or sys.executable + ' -m coinjure.cli.cli'


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _socket_status(socket_path: str) -> dict:
    try:
        return run_command('status', socket_path=Path(socket_path))
    except FileNotFoundError:
        return {'ok': False, 'error': 'socket_not_found'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


async def _gather_socket_statuses(
    entries: list[StrategyEntry],
) -> list[tuple[StrategyEntry, dict]]:
    async def _query(entry: StrategyEntry) -> tuple[StrategyEntry, dict]:
        if not entry.socket_path:
            return entry, {'ok': False, 'error': 'no_socket_path'}
        return entry, await asyncio.to_thread(_socket_status, entry.socket_path)

    active = [e for e in entries if e.lifecycle in ('paper_trading', 'live_trading')]
    if not active:
        return []
    results = await asyncio.gather(*[_query(e) for e in active])
    return list(results)


def _print_response(resp: dict, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(resp))
        return
    if resp.get('ok'):
        status = resp.get('status') or resp.get('error') or 'ok'
        click.echo(status)
    else:
        click.echo(f"error: {resp.get('error', 'unknown')}")


def _resolve_socket_for_id(strategy_id: str | None, socket: str | None) -> Path:
    """Resolve socket path: --id looks up registry, --socket uses direct path."""
    if socket:
        return Path(socket)
    if strategy_id:
        reg = _load_registry()
        entry = reg.get(strategy_id)
        if entry and entry.socket_path:
            return Path(entry.socket_path)
        # Fallback convention
        return SOCKET_DIR / f'{strategy_id}.sock'
    return SOCKET_PATH


def _resolve_kill_switch_file(path: str | None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get('PRED_MARKET_CLI_KILL_SWITCH_FILE', '').strip()
    if env_path:
        return Path(env_path)
    return Path.home() / '.coinjure' / 'kill.switch'


def _load_relations_for_batch(status: str) -> list:
    from coinjure.market.relations import RelationStore

    return RelationStore().list(status=status)


def _get_relation_store_path() -> Path:
    from coinjure.market.relations import RELATIONS_PATH

    return RELATIONS_PATH


def _ensure_hub_running(as_json: bool) -> None:
    """Auto-start hub if not running. Waits up to 5s for socket."""
    if HUB_SOCKET_PATH.exists():
        return
    cmd = shlex.split(_coinjure_cmd()) + ['hub', 'start', '--detach']
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if HUB_SOCKET_PATH.exists():
            return
        time.sleep(0.1)
    if not as_json:
        click.echo('Warning: Hub socket not ready after 5s, proceeding anyway.')


def _run_batch_paper(
    *,
    initial_capital: str,
    duration: float | None,
    as_json: bool,
    no_hub: bool,
) -> None:
    """Spawn one detached paper-run per backtest_passed relation."""
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    relations = _load_relations_for_batch('backtest_passed')
    if not relations:
        raise click.ClickException('No relations with status backtest_passed.')

    if not no_hub:
        _ensure_hub_running(as_json=as_json)

    reg = _load_registry()
    results = []

    for rel in relations:
        ref, kwargs = build_strategy_ref_for_relation(rel)
        if ref is None:
            results.append(
                {
                    'relation_id': rel.relation_id,
                    'ok': False,
                    'error': f'No strategy for spread_type: {rel.spread_type}',
                }
            )
            continue

        cmd = shlex.split(_coinjure_cmd()) + [
            'engine',
            'paper-run',
            '--exchange',
            'cross_platform',
            '--strategy-ref',
            ref,
            '--strategy-kwargs-json',
            json.dumps(kwargs),
            '--initial-capital',
            initial_capital,
            '--no-detach',
        ]
        if duration is not None:
            cmd += ['--duration', str(duration)]
        if no_hub:
            cmd += ['--no-hub']

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        socket = str(SOCKET_DIR / f'engine-{proc.pid}.sock')
        entry = StrategyEntry(
            strategy_id=rel.relation_id,
            strategy_ref=ref,
            strategy_kwargs=kwargs,
            relation_id=rel.relation_id,
            lifecycle='paper_trading',
            exchange='cross_platform',
            pid=proc.pid,
            socket_path=socket,
        )
        try:
            reg.add(entry)
        except ValueError:
            entry_existing = reg.get(rel.relation_id)
            if entry_existing:
                entry_existing.pid = proc.pid
                entry_existing.socket_path = socket
                entry_existing.lifecycle = 'paper_trading'
                reg.update(entry_existing)

        results.append(
            {
                'relation_id': rel.relation_id,
                'ok': True,
                'pid': proc.pid,
                'strategy_ref': ref,
                'socket': socket,
            }
        )

    if as_json:
        _emit_json({'ok': True, 'launched': results, 'count': len(results)})
    else:
        click.echo(f'\nLaunched {len(results)} paper trading instances:\n')
        for r in results:
            if r.get('ok'):
                click.echo(f'  {r["relation_id"]}  pid={r["pid"]}  {r["strategy_ref"]}')
            else:
                click.echo(f'  {r["relation_id"]}  SKIPPED: {r["error"]}')


def _run_batch_live(
    *,
    initial_capital: str,
    duration: float | None,
    exchange: str,
    as_json: bool,
) -> None:
    """Spawn one detached live-run per deployed relation."""
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    relations = _load_relations_for_batch('deployed')
    if not relations:
        raise click.ClickException('No relations with status deployed.')

    _ensure_hub_running(as_json=as_json)

    reg = _load_registry()
    results = []

    for rel in relations:
        ref, kwargs = build_strategy_ref_for_relation(rel)
        if ref is None:
            results.append(
                {
                    'relation_id': rel.relation_id,
                    'ok': False,
                    'error': f'No strategy for spread_type: {rel.spread_type}',
                }
            )
            continue

        cmd = shlex.split(_coinjure_cmd()) + [
            'engine',
            'live-run',
            '--strategy-ref',
            ref,
            '--strategy-kwargs-json',
            json.dumps(kwargs),
            '--exchange',
            exchange,
            '--no-detach',
        ]
        if duration is not None:
            cmd += ['--duration', str(duration)]

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        socket = str(SOCKET_DIR / f'engine-{proc.pid}.sock')
        entry = StrategyEntry(
            strategy_id=rel.relation_id,
            strategy_ref=ref,
            strategy_kwargs=kwargs,
            relation_id=rel.relation_id,
            lifecycle='live_trading',
            exchange=exchange,
            pid=proc.pid,
            socket_path=socket,
        )
        try:
            reg.add(entry)
        except ValueError:
            entry_existing = reg.get(rel.relation_id)
            if entry_existing:
                entry_existing.pid = proc.pid
                entry_existing.socket_path = socket
                entry_existing.lifecycle = 'live_trading'
                reg.update(entry_existing)

        results.append(
            {
                'relation_id': rel.relation_id,
                'ok': True,
                'pid': proc.pid,
                'strategy_ref': ref,
                'socket': socket,
            }
        )

    if as_json:
        _emit_json({'ok': True, 'launched': results, 'count': len(results)})
    else:
        click.echo(f'\nLaunched {len(results)} live trading instances:\n')
        for r in results:
            if r.get('ok'):
                click.echo(f'  {r["relation_id"]}  pid={r["pid"]}  {r["strategy_ref"]}')
            else:
                click.echo(f'  {r["relation_id"]}  SKIPPED: {r["error"]}')


# ── Click group ────────────────────────────────────────────────────────────────


@click.group()
def engine() -> None:
    """Running engine instances — paper & live trading, management, batch ops."""


# ── paper-run ─────────────────────────────────────────────────────────────────


@engine.command('paper-run')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'cross_platform']),
    default='polymarket',
    show_default=True,
)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode.',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
@click.option(
    '--socket-path',
    default=None,
    type=click.Path(),
    help='Unix socket path for the control server.',
)
@click.option(
    '--no-hub',
    is_flag=True,
    default=False,
    help='Skip auto-connecting to the Market Data Hub even if running.',
)
@click.option(
    '--all-relations',
    is_flag=True,
    default=False,
    help='Batch run all backtest_passed relations (each as a detached process).',
)
@click.option(
    '--detach/--no-detach',
    default=False,
    help='Run as a detached background process.',
)
def engine_paper_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str | None,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
    socket_path: str | None,
    no_hub: bool,
    all_relations: bool,
    detach: bool,
) -> None:
    """Run a paper trading engine instance."""
    if all_relations and strategy_ref:
        raise click.ClickException(
            '--all-relations and --strategy-ref are mutually exclusive.'
        )

    if all_relations:
        _run_batch_paper(
            initial_capital=initial_capital,
            duration=duration,
            as_json=as_json,
            no_hub=no_hub,
        )
        return

    if detach:
        cmd = shlex.split(_coinjure_cmd()) + ['engine', 'paper-run', '--no-detach']
        if strategy_ref:
            cmd += ['--strategy-ref', strategy_ref]
        if strategy_kwargs_json:
            cmd += ['--strategy-kwargs-json', strategy_kwargs_json]
        cmd += ['--exchange', exchange, '--initial-capital', initial_capital]
        if duration is not None:
            cmd += ['--duration', str(duration)]
        if no_hub:
            cmd += ['--no-hub']
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Register in portfolio
        reg = _load_registry()
        sid = strategy_ref or f'paper-{proc.pid}'
        entry = StrategyEntry(
            strategy_id=sid,
            strategy_ref=strategy_ref or 'idle',
            strategy_kwargs=_parse_strategy_kwargs_json(strategy_kwargs_json),
            lifecycle='paper_trading',
            exchange=exchange,
            pid=proc.pid,
            socket_path=str(SOCKET_DIR / f'engine-{proc.pid}.sock'),
        )
        try:
            reg.add(entry)
        except ValueError:
            reg.update(entry)

        _emit(
            {
                'ok': True,
                'pid': proc.pid,
                'strategy_id': sid,
                'socket': entry.socket_path,
            },
            as_json=as_json,
        )
        return

    from coinjure.engine.runner import (
        run_live_kalshi_paper_trading,
        run_live_paper_trading,
    )

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    capital = Decimal(initial_capital)
    _socket_path = Path(socket_path) if socket_path else None

    exchange_label = {
        'polymarket': 'Polymarket',
        'kalshi': 'Kalshi',
        'cross_platform': 'Cross-Platform',
    }.get(exchange, exchange)

    _emit(
        {
            'mode': 'paper',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'no_hub': no_hub,
            'message': (
                f'Starting paper mode ({exchange})'
                if strategy_ref
                else f'Starting paper mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    hub_socket = HUB_SOCKET_PATH if (not no_hub and HUB_SOCKET_PATH.exists()) else None
    if hub_socket:
        from coinjure.data.live.polymarket import LiveRSSNewsDataSource
        from coinjure.data.source import CompositeDataSource
        from coinjure.hub.subscriber import HubDataSource

        data_source = CompositeDataSource(
            [HubDataSource(hub_socket), LiveRSSNewsDataSource()]
        )
    else:
        data_source = _build_market_source(exchange)

    if exchange == 'kalshi':
        asyncio.run(
            run_live_kalshi_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name=exchange_label,
                emit_text=not as_json,
                socket_path=_socket_path,
            )
        )
    else:
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name=exchange_label,
                emit_text=not as_json,
                socket_path=_socket_path,
            )
        )

    _emit({'mode': 'paper', 'message': 'Paper session ended'}, as_json=as_json)


# ── live-run ──────────────────────────────────────────────────────────────────


@engine.command('live-run')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class.',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
@click.option(
    '--wallet-private-key',
    default=None,
    help='Polymarket wallet private key (or POLYMARKET_PRIVATE_KEY).',
)
@click.option('--signature-type', default=0, show_default=True, type=int)
@click.option('--funder', default=None, help='Polymarket funder wallet.')
@click.option('--kalshi-api-key-id', default=None, help='Kalshi API key id.')
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path.',
)
@click.option(
    '--all-relations',
    is_flag=True,
    default=False,
    help='Batch run all deployed relations (each as a detached process).',
)
@click.option(
    '--detach/--no-detach',
    default=False,
    help='Run as a detached background process.',
)
def engine_live_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str | None,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
    wallet_private_key: str | None,
    signature_type: int,
    funder: str | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    all_relations: bool,
    detach: bool,
) -> None:
    """Run a live trading engine instance (real orders, real funds)."""
    if all_relations and strategy_ref:
        raise click.ClickException(
            '--all-relations and --strategy-ref are mutually exclusive.'
        )

    _confirm_live_trading(as_json=as_json)

    if all_relations:
        _run_batch_live(
            initial_capital=initial_capital,
            duration=duration,
            exchange=exchange,
            as_json=as_json,
        )
        return

    if not strategy_ref:
        raise click.ClickException(
            '--strategy-ref is required unless using --all-relations.'
        )

    if detach:
        cmd = shlex.split(_coinjure_cmd()) + [
            'engine',
            'live-run',
            '--no-detach',
            '--strategy-ref',
            strategy_ref,
            '--exchange',
            exchange,
        ]
        if strategy_kwargs_json:
            cmd += ['--strategy-kwargs-json', strategy_kwargs_json]
        if duration is not None:
            cmd += ['--duration', str(duration)]

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        reg = _load_registry()
        sid = strategy_ref
        entry = StrategyEntry(
            strategy_id=sid,
            strategy_ref=strategy_ref,
            strategy_kwargs=_parse_strategy_kwargs_json(strategy_kwargs_json),
            lifecycle='live_trading',
            exchange=exchange,
            pid=proc.pid,
            socket_path=str(SOCKET_DIR / f'engine-{proc.pid}.sock'),
        )
        try:
            reg.add(entry)
        except ValueError:
            reg.update(entry)

        _emit(
            {
                'ok': True,
                'pid': proc.pid,
                'strategy_id': sid,
                'socket': entry.socket_path,
            },
            as_json=as_json,
        )
        return

    from coinjure.data.live.kalshi import LiveKalshiDataSource
    from coinjure.data.live.polymarket import LivePolyMarketDataSource
    from coinjure.engine.runner import (
        run_live_kalshi_trading,
        run_live_polymarket_trading,
    )

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)

    _emit(
        {
            'mode': 'live',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'message': f'Starting live mode ({exchange})',
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        private_key = wallet_private_key or os.environ.get('POLYMARKET_PRIVATE_KEY')
        if not private_key:
            raise click.ClickException(
                'Missing Polymarket key. Pass --wallet-private-key or set POLYMARKET_PRIVATE_KEY.'
            )
        data_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_polymarket_trading(
                data_source=data_source,
                strategy=strategy_obj,
                wallet_private_key=private_key,
                signature_type=signature_type,
                funder=funder,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Polymarket',
            )
        )
    else:
        kalshi_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_kalshi_trading(
                data_source=kalshi_source,
                strategy=strategy_obj,
                api_key_id=kalshi_api_key_id,
                private_key_path=kalshi_private_key_path,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Kalshi',
            )
        )

    _emit({'mode': 'live', 'message': 'Live session ended'}, as_json=as_json)


# ── list ───────────────────────────────────────────────────────────────────────


@engine.command('list')
@click.option('--lifecycle', default=None, help='Filter by lifecycle stage.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_list(lifecycle: str | None, as_json: bool) -> None:
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


@engine.command('add')
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
def engine_add(
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


# ── Per-instance commands with --id resolution ────────────────────────────────


@engine.command('status')
@click.option(
    '--id',
    'strategy_id',
    default=None,
    help='Strategy ID (resolves socket via registry).',
)
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--full',
    is_flag=True,
    default=False,
    help='Full snapshot including positions, PnL, decisions, and order books.',
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_status(
    strategy_id: str | None,
    socket: str | None,
    full: bool,
    as_json: bool,
) -> None:
    """Show engine runtime status. Use --full for complete snapshot."""
    # Single-engine mode: --id or --socket specified
    if strategy_id or socket:
        sock = _resolve_socket_for_id(strategy_id, socket)
        cmd = 'get_state' if full else 'status'
        resp = run_command(cmd, socket_path=sock)
        if full:
            _print_response(resp, as_json)
            if not resp.get('ok'):
                raise SystemExit(1)
            return
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
        return

    # All-engines mode: no --id/--socket → query every active registry entry
    reg = _load_registry()
    entries = reg.list()
    results = asyncio.run(_gather_socket_statuses(entries))

    if not results:
        if as_json:
            click.echo(json.dumps([]))
        else:
            click.echo('No running engines.')
        return

    if as_json:
        out = [{'strategy_id': entry.strategy_id, **resp} for entry, resp in results]
        click.echo(json.dumps(out))
        return

    for entry, resp in results:
        if resp.get('ok'):
            click.echo(
                '{sid}  paused={paused} runtime={runtime} events={events} '
                'decisions={decisions} executed={executed} orders={orders}'.format(
                    sid=entry.strategy_id,
                    paused=resp.get('paused', False),
                    runtime=resp.get('runtime', '0:00:00'),
                    events=resp.get('event_count', 0),
                    decisions=resp.get('decisions', 0),
                    executed=resp.get('executed', 0),
                    orders=resp.get('orders', 0),
                )
            )
        else:
            click.echo(f'{entry.strategy_id}  error={resp.get("error", "unknown")}')


@engine.command('pause')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--all',
    'all_flag',
    is_flag=True,
    default=False,
    help='Pause all running instances.',
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_pause(
    strategy_id: str | None,
    socket: str | None,
    all_flag: bool,
    as_json: bool,
) -> None:
    """Pause the engine. Use --all to pause every running instance."""
    if all_flag:
        reg = _load_registry()
        active = [
            e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')
        ]
        results: list[dict] = []
        for entry in active:
            if not entry.socket_path:
                results.append(
                    {'id': entry.strategy_id, 'ok': False, 'error': 'no_socket_path'}
                )
                continue
            try:
                resp = run_command('pause', socket_path=Path(entry.socket_path))
                results.append({'id': entry.strategy_id, **resp})
            except Exception as exc:
                results.append(
                    {'id': entry.strategy_id, 'ok': False, 'error': str(exc)}
                )
        payload = {'ok': True, 'count': len(results), 'results': results}
        if as_json:
            _emit_json(payload)
        else:
            for r in results:
                status = 'OK' if r.get('ok') else f'FAIL: {r.get("error")}'
                click.echo(f'  {r["id"]}: {status}')
        return
    sock = _resolve_socket_for_id(strategy_id, socket)
    resp = run_command('pause', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@engine.command('resume')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_resume(strategy_id: str | None, socket: str | None, as_json: bool) -> None:
    """Resume decision-making."""
    sock = _resolve_socket_for_id(strategy_id, socket)
    resp = run_command('resume', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@engine.command('stop')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--all', 'all_flag', is_flag=True, default=False, help='Stop all running instances.'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_stop(
    strategy_id: str | None,
    socket: str | None,
    all_flag: bool,
    as_json: bool,
) -> None:
    """Gracefully stop the engine. Use --all to stop every running instance."""
    if all_flag:
        reg = _load_registry()
        active = [
            e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')
        ]
        results: list[dict] = []
        for entry in active:
            if not entry.socket_path:
                results.append(
                    {'id': entry.strategy_id, 'ok': False, 'error': 'no_socket_path'}
                )
                continue
            try:
                resp = run_command('stop', socket_path=Path(entry.socket_path))
                results.append({'id': entry.strategy_id, **resp})
            except Exception as exc:
                results.append(
                    {'id': entry.strategy_id, 'ok': False, 'error': str(exc)}
                )
        payload = {'ok': True, 'count': len(results), 'results': results}
        if as_json:
            _emit_json(payload)
        else:
            for r in results:
                status = 'OK' if r.get('ok') else f'FAIL: {r.get("error")}'
                click.echo(f'  {r["id"]}: {status}')
        return
    sock = _resolve_socket_for_id(strategy_id, socket)
    resp = run_command('stop', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@engine.command('swap')
@click.option(
    '--strategy-ref',
    required=True,
    help='Strategy ref: module:Class or /path/file.py:Class',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_swap(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    strategy_id: str | None,
    socket: str | None,
    as_json: bool,
) -> None:
    """Hot-swap the running engine's strategy without restarting."""
    kwargs: dict = {}
    if strategy_kwargs_json:
        try:
            kwargs = json.loads(strategy_kwargs_json)
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f'Invalid --strategy-kwargs-json: {exc.msg}'
            ) from exc
        if not isinstance(kwargs, dict):
            raise click.ClickException('--strategy-kwargs-json must be a JSON object.')

    sock = _resolve_socket_for_id(strategy_id, socket)
    resp = run_command(
        'swap_strategy', socket_path=sock, strategy_ref=strategy_ref, kwargs=kwargs
    )
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


# ── retire ─────────────────────────────────────────────────────────────────────


@engine.command('retire')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option('--reason', default='manual', show_default=True)
@click.option(
    '--all',
    'all_flag',
    is_flag=True,
    default=False,
    help='Retire all active strategies.',
)
@click.option(
    '--lifecycle',
    default=None,
    help='Filter by lifecycle when using --all (paper_trading|live_trading).',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_retire(
    strategy_id: str | None,
    reason: str,
    all_flag: bool,
    lifecycle: str | None,
    as_json: bool,
) -> None:
    """Stop a running strategy and mark it as retired. Use --all to retire all."""
    if all_flag:
        reg = _load_registry()
        active = [
            e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')
        ]
        if lifecycle:
            active = [e for e in active if e.lifecycle == lifecycle]
        results: list[dict] = []
        for entry in active:
            stop_result: dict = {'ok': True, 'status': 'not_running'}
            if entry.socket_path and Path(entry.socket_path).exists():
                try:
                    stop_result = run_command(
                        'stop', socket_path=Path(entry.socket_path)
                    )
                except Exception as exc:
                    stop_result = {'ok': False, 'error': str(exc)}
            entry.lifecycle = 'retired'
            entry.retired_reason = reason
            entry.pid = None
            entry.socket_path = None
            reg.update(entry)
            results.append(
                {'id': entry.strategy_id, 'ok': True, 'stop_result': stop_result}
            )
        payload = {'ok': True, 'count': len(results), 'results': results}
        if as_json:
            _emit_json(payload)
        else:
            for r in results:
                click.echo(f'  Retired {r["id"]}')
        return

    if not strategy_id:
        raise click.ClickException('Provide --id or use --all.')

    reg = _load_registry()
    entry = reg.get(strategy_id)
    if entry is None:
        raise click.ClickException(f'Strategy not found: {strategy_id!r}')

    stop_result_single: dict = {'ok': True, 'status': 'not_running'}

    if entry.socket_path and Path(entry.socket_path).exists():
        try:
            run_command('status', socket_path=Path(entry.socket_path))
        except Exception:  # noqa: BLE001
            pass

    if entry.socket_path and Path(entry.socket_path).exists():
        try:
            stop_result_single = run_command(
                'stop', socket_path=Path(entry.socket_path)
            )
        except Exception as exc:
            stop_result_single = {'ok': False, 'error': str(exc)}

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
        'stop_result': stop_result_single,
    }
    if as_json:
        _emit_json(resp)
    else:
        click.echo(f'Retired {strategy_id!r}: {reason}')


_STALE_DAYS = 7
_NO_SIGNAL_HOURS = 24


# ── monitor ────────────────────────────────────────────────────────────────────


@engine.command('monitor')
@click.option(
    '--socket',
    '-s',
    default=None,
    type=click.Path(),
    help='Path to a single engine control socket. Omit to auto-discover all running engines.',
)
def engine_monitor(socket: str | None) -> None:
    """Attach a live Textual monitor to all running engines (or a specific one with -s)."""
    from coinjure.cli.textual_monitor import SocketTradingMonitorApp

    if socket:
        socket_paths = [Path(socket)]
        socket_labels: dict = {}
    else:
        # Auto-discover all running engines from the registry
        registry = _load_registry()
        socket_labels = {}
        socket_paths = []
        for entry in registry.list():
            if entry.socket_path and Path(entry.socket_path).exists():
                p = Path(entry.socket_path)
                socket_paths.append(p)
                socket_labels[p] = entry.strategy_id
        if not socket_paths:
            # Fallback: scan SOCKET_DIR for any live engine sockets (covers
            # foreground / non-registered engines that use PID-based paths)
            socket_paths = sorted(SOCKET_DIR.glob('*.sock')) or [SOCKET_PATH]

    try:
        app = SocketTradingMonitorApp(
            socket_paths=socket_paths, socket_labels=socket_labels
        )
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        click.echo(f'Monitor closed: {exc}', err=True)


# ── killswitch ─────────────────────────────────────────────────────────────────


@engine.command('killswitch')
@click.option(
    '--on', 'enable', is_flag=True, default=False, help='Enable global kill-switch'
)
@click.option(
    '--off', 'disable', is_flag=True, default=False, help='Disable global kill-switch'
)
@click.option('--path', default=None, type=click.Path(), help='Kill-switch file path')
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_killswitch(
    enable: bool, disable: bool, path: str | None, as_json: bool
) -> None:
    """Toggle/query global kill-switch file used by all traders."""
    if enable and disable:
        raise click.ClickException('Use either --on or --off, not both.')
    kill_file = _resolve_kill_switch_file(path)
    kill_file.parent.mkdir(parents=True, exist_ok=True)

    if enable:
        kill_file.write_text('1\n')
        resp = {'ok': True, 'status': 'enabled', 'path': str(kill_file)}
    elif disable:
        kill_file.unlink(missing_ok=True)
        resp = {'ok': True, 'status': 'disabled', 'path': str(kill_file)}
    else:
        resp = {
            'ok': True,
            'status': 'enabled' if kill_file.exists() else 'disabled',
            'path': str(kill_file),
        }

    _print_response(resp, as_json)


@engine.command('report')
@click.option(
    '--check-health',
    is_flag=True,
    default=False,
    help='Include health diagnostics (stale, degraded, dead processes).',
)
@click.option(
    '--update/--no-update',
    default=True,
    show_default=True,
    help='Write back PnL / last_signal_at to registry (with --check-health).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_report(check_health: bool, update: bool, as_json: bool) -> None:
    """Aggregated PnL + ranking across all active engines.

    Use --check-health to also detect stale, degraded, or dead instances.
    """
    reg = _load_registry()
    entries = reg.list()
    active = [e for e in entries if e.lifecycle in ('paper_trading', 'live_trading')]

    if not active:
        payload: dict[str, Any] = {'ok': True, 'active': 0, 'engines': []}
        if as_json:
            _emit_json(payload)
        else:
            click.echo('No active engines.')
        return

    results = asyncio.run(_gather_socket_statuses(entries))
    status_map = {e.strategy_id: s for e, s in results}

    engines: list[dict] = []
    for entry, status in results:
        portfolio_data = status.get('portfolio', {})
        total_val = portfolio_data.get('total')
        realized = portfolio_data.get('realized_pnl')
        engines.append(
            {
                'id': entry.strategy_id,
                'lifecycle': entry.lifecycle,
                'ok': status.get('ok', False),
                'total_value': total_val,
                'realized_pnl': realized,
                'event_count': status.get('event_count', 0),
                'orders': status.get('orders', 0),
                'runtime': status.get('runtime'),
            }
        )

    def _pnl_key(e: dict) -> float:
        try:
            return float(e.get('realized_pnl') or 0)
        except (TypeError, ValueError):
            return 0.0

    engines.sort(key=_pnl_key, reverse=True)

    payload_dict: dict[str, Any] = {
        'ok': True,
        'active': len(active),
        'engines': engines,
    }

    # ── Health diagnostics ────────────────────────────────────────────
    if check_health:
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

            if entry.pid is not None and not _is_pid_alive(entry.pid):
                dead_process.append(
                    {**entry_info, 'reason': 'pid_not_found', 'pid': entry.pid}
                )
                continue

            status = status_map.get(sid, {})
            if not status.get('ok'):
                dead_process.append(
                    {
                        **entry_info,
                        'reason': 'socket_unreachable',
                        'error': status.get('error'),
                    }
                )
                continue

            if update:
                portfolio_val = (status.get('portfolio') or {}).get('total')
                if portfolio_val is not None:
                    entry.paper_pnl = str(round(float(portfolio_val) - 10000, 2))
                last_activity = status.get('last_activity') or ''
                if last_activity:
                    entry.last_signal_at = last_activity
                reg.update(entry)

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

        payload_dict['health'] = {
            'stale': stale,
            'degraded': degraded,
            'dead_process': dead_process,
            'healthy': healthy,
            'summary': {
                'total_active': len(active),
                'healthy': len(healthy),
                'issues': len(stale) + len(degraded) + len(dead_process),
            },
        }

    if as_json:
        _emit_json(payload_dict)
        return

    click.echo(f'\nEngine Report — {len(active)} active\n')
    for e in engines:
        status_str = 'OK' if e['ok'] else 'UNREACHABLE'
        pnl_str = e.get('realized_pnl', '?')
        click.echo(f'  {e["id"]}  [{status_str}]  pnl={pnl_str}  orders={e["orders"]}')

    if check_health:
        health = payload_dict['health']

        def _section(label: str, items: list[dict]) -> None:
            if not items:
                return
            click.echo(f'\n{label} ({len(items)}):')
            for item in items:
                click.echo(f'  - {item["id"]}: {item.get("reason", "")}')

        _section('Dead processes', health['dead_process'])
        _section('Stale (no recent signal)', health['stale'])
        _section('Degraded (consecutive losses)', health['degraded'])
        _section('Healthy', health['healthy'])

        summary = health['summary']
        click.echo(
            f'\nHealth: {summary["total_active"]} active, '
            f'{summary["healthy"]} healthy, '
            f'{summary["issues"]} issues'
        )

    click.echo()


# ── allocate ──────────────────────────────────────────────────────────────────


@engine.command('allocate')
@click.option(
    '--method',
    type=click.Choice(['equal', 'edge', 'kelly']),
    default='equal',
    show_default=True,
    help='Allocation method.',
)
@click.option(
    '--max-exposure',
    default='50000',
    show_default=True,
    help='Max total capital deployed.',
)
@click.option(
    '--max-per-strategy',
    default='5000',
    show_default=True,
    help='Max capital per strategy.',
)
@click.option(
    '--execute/--no-execute',
    default=False,
    show_default=True,
    help='Apply allocation changes.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_allocate(
    method: str,
    max_exposure: str,
    max_per_strategy: str,
    execute: bool,
    as_json: bool,
) -> None:
    """Allocate capital across active strategies.

    Methods:
      equal — divide evenly among active strategies.
      edge — weight by backtest edge (higher edge → more capital).
      kelly — fractional Kelly criterion based on win rate and edge.
    """
    max_exp = Decimal(max_exposure)
    max_per = Decimal(max_per_strategy)

    reg = _load_registry()
    active = [e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')]

    if not active:
        if as_json:
            _emit_json(
                {'ok': True, 'allocations': [], 'message': 'no active strategies'}
            )
        else:
            click.echo('No active strategies to allocate.')
        return

    # Gather metrics for edge/kelly
    socket_statuses = asyncio.run(_gather_socket_statuses(active))
    status_map = {e.strategy_id: s for e, s in socket_statuses}

    allocations: list[dict] = []

    if method == 'equal':
        per_strategy = min(max_exp / len(active), max_per)
        for entry in active:
            allocations.append(
                {
                    'strategy_id': entry.strategy_id,
                    'allocation': str(per_strategy.quantize(Decimal('0.01'))),
                    'method': 'equal',
                }
            )

    elif method == 'edge':
        # Weight by absolute paper PnL as proxy for edge
        edges: list[tuple[StrategyEntry, Decimal]] = []
        for entry in active:
            pnl = Decimal(str(entry.paper_pnl or '0'))
            edges.append((entry, max(pnl, Decimal('0.01'))))  # floor to avoid zero

        total_edge = sum(e for _, e in edges)
        for entry, edge in edges:
            weight = edge / total_edge
            alloc = min(weight * max_exp, max_per)
            allocations.append(
                {
                    'strategy_id': entry.strategy_id,
                    'allocation': str(alloc.quantize(Decimal('0.01'))),
                    'weight': str(weight.quantize(Decimal('0.0001'))),
                    'method': 'edge',
                }
            )

    elif method == 'kelly':
        for entry in active:
            status = status_map.get(entry.strategy_id, {})
            ds = status.get('decision_stats', {})
            win_rate = float(ds.get('win_rate', 0.5))
            avg_win = float(ds.get('avg_win', 0.01))
            avg_loss = float(ds.get('avg_loss', 0.01)) or 0.01

            # Kelly fraction: f* = (p*b - q) / b where b=avg_win/avg_loss
            b = abs(avg_win / avg_loss)
            q = 1.0 - win_rate
            kelly_f = (win_rate * b - q) / b if b > 0 else 0
            # Half-Kelly for safety
            kelly_f = max(0, min(kelly_f * 0.5, 0.25))

            alloc = min(Decimal(str(kelly_f)) * max_exp, max_per)
            allocations.append(
                {
                    'strategy_id': entry.strategy_id,
                    'allocation': str(alloc.quantize(Decimal('0.01'))),
                    'kelly_fraction': round(kelly_f, 4),
                    'win_rate': round(win_rate, 4),
                    'method': 'kelly',
                }
            )

    # Cap total exposure
    total_alloc = sum(Decimal(a['allocation']) for a in allocations)
    if total_alloc > max_exp:
        scale = max_exp / total_alloc
        for a in allocations:
            a['allocation'] = str(
                (Decimal(a['allocation']) * scale).quantize(Decimal('0.01'))
            )

    if as_json:
        _emit_json({'ok': True, 'allocations': allocations, 'total': str(total_alloc)})
        return

    click.echo(f'\nCapital Allocation ({method}) — {len(active)} strategies\n')
    for a in allocations:
        extra = ''
        if 'weight' in a:
            extra = f'  weight={a["weight"]}'
        elif 'kelly_fraction' in a:
            extra = f'  kelly={a["kelly_fraction"]}  wr={a["win_rate"]}'
        click.echo(f'  {a["strategy_id"]}: ${a["allocation"]}{extra}')

    total = sum(Decimal(a['allocation']) for a in allocations)
    click.echo(f'\n  Total: ${total}  (max: ${max_exposure})')

    if not execute:
        click.echo('\n  (dry run — use --execute to apply)')
    else:
        click.echo('\n  Allocation recorded.')


# ── backtest ──────────────────────────────────────────────────────────────────


@engine.command('backtest')
@click.option(
    '--relation',
    'relation_ids',
    multiple=True,
    help='Relation ID to backtest. Repeat for multiple.',
)
@click.option(
    '--all-relations',
    is_flag=True,
    default=False,
    help='Backtest all active relations (skips already-tested by default).',
)
@click.option(
    '--rerun',
    is_flag=True,
    default=False,
    help='Re-run all relations including already backtest_passed/backtest_failed.',
)
@click.option(
    '--parquet',
    'parquet_paths',
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help='Parquet orderbook file(s) as data source override.',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON output')
def engine_backtest(
    relation_ids: tuple[str, ...],
    all_relations: bool,
    rerun: bool,
    parquet_paths: tuple[str, ...],
    initial_capital: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
) -> None:
    """Backtest relations using auto-selected strategies.

    Each relation's spread_type determines the strategy. Results update the
    relation lifecycle (backtest_passed / backtest_failed) in the store.
    """
    from coinjure.engine.backtester import run_backtest_relation
    from coinjure.market.relations import RelationStore

    if not relation_ids and not all_relations:
        raise click.ClickException('Provide --relation <id> or --all-relations')

    if rerun and not all_relations:
        raise click.ClickException('--rerun only works with --all-relations')

    store = RelationStore()
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    capital = Decimal(initial_capital)
    parquet = (
        list(parquet_paths)
        if len(parquet_paths) > 1
        else parquet_paths[0]
        if parquet_paths
        else None
    )

    # Resolve relations
    if all_relations:
        if rerun:
            relations = [r for r in store.list() if r.status != 'retired']
        else:
            relations = [r for r in store.list() if r.status == 'active']
    else:
        relations = []
        for rid in relation_ids:
            r = store.get(rid)
            if r is None:
                raise click.ClickException(f'Relation not found: {rid}')
            relations.append(r)

    if not relations:
        if all_relations and not rerun:
            total = len([r for r in store.list() if r.status != 'retired'])
            raise click.ClickException(
                f'No untested relations (all {total} already tested). '
                f'Use --rerun to re-test.'
            )
        raise click.ClickException('No relations to backtest')

    if all_relations and not rerun:
        skipped = len([r for r in store.list() if r.status != 'retired']) - len(
            relations
        )
        if skipped > 0:
            click.echo(
                f'Skipping {skipped} already-tested relation(s). Use --rerun to include them.'
            )

    _emit(
        {
            'mode': 'backtest',
            'message': f'Backtesting {len(relations)} relation(s)',
            'relation_count': len(relations),
        },
        as_json=as_json,
    )

    async def _run_all():
        results = []
        for rel in relations:
            result = await run_backtest_relation(
                rel,
                initial_capital=capital,
                parquet_path=parquet,
                strategy_kwargs=strategy_kwargs,
            )
            # Update relation lifecycle
            if result.error is None:
                rel.set_backtest_result(
                    passed=result.passed,
                    pnl=float(result.total_pnl),
                    trades=result.trade_count,
                )
                store.update(rel)
            results.append(result)
        return results

    results = asyncio.run(_run_all())

    # Output results
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and r.error is None)
    errors = sum(1 for r in results if r.error is not None)

    if as_json:
        _emit_json(
            [
                {
                    'relation_id': r.relation_id,
                    'spread_type': r.spread_type,
                    'strategy': r.strategy_name,
                    'pnl': float(r.total_pnl),
                    'trades': r.trade_count,
                    'passed': r.passed,
                    'error': r.error,
                }
                for r in results
            ]
        )
    else:
        click.echo(
            f'\n  Backtest results: {passed} passed, {failed} failed, {errors} errors\n'
        )
        for r in results:
            status = 'PASS' if r.passed else ('ERROR' if r.error else 'FAIL')
            pnl_str = f'pnl={r.total_pnl:+.2f}' if r.error is None else r.error
            click.echo(
                f'  [{status}] {r.relation_id[:20]}  '
                f'{r.spread_type}  {r.strategy_name}  '
                f'{pnl_str}  trades={r.trade_count}'
            )


# ── Promote ────────────────────────────────────────────────────────────────────


@engine.command('promote')
@click.argument('relation_id', required=False, default=None)
@click.option(
    '--all',
    'promote_all',
    is_flag=True,
    default=False,
    help='Promote all paper_trading entries with positive PnL.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_promote(
    relation_id: str | None,
    promote_all: bool,
    as_json: bool,
) -> None:
    """Promote relation(s) from paper_trading to deployed."""
    from coinjure.market.relations import RelationStore

    if not relation_id and not promote_all:
        raise click.ClickException('Provide <relation-id> or --all.')

    store = RelationStore(path=_get_relation_store_path())
    reg = _load_registry()

    if promote_all:
        entries = [
            e for e in reg.list() if e.lifecycle == 'paper_trading' and e.relation_id
        ]
        promoted = []
        for entry in entries:
            rel = store.get(entry.relation_id)
            if rel is None:
                continue
            rel.status = 'deployed'
            store.update(rel)
            entry.lifecycle = 'deployed'
            reg.update(entry)
            promoted.append(entry.relation_id)

        if as_json:
            _emit_json({'ok': True, 'promoted': promoted, 'count': len(promoted)})
        else:
            click.echo(f'Promoted {len(promoted)} relation(s) to deployed.')
        return

    # Single relation
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    rel.status = 'deployed'
    store.update(rel)

    entry = reg.get(relation_id)
    if entry:
        entry.lifecycle = 'deployed'
        reg.update(entry)

    if as_json:
        _emit_json({'ok': True, 'relation_id': relation_id, 'status': 'deployed'})
    else:
        click.echo(f'Promoted {relation_id} to deployed.')
