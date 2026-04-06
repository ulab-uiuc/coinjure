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


# ── Audit log ─────────────────────────────────────────────────────────────────

_AUDIT_DIR = Path.home() / '.coinjure'
_AUDIT_FILE = _AUDIT_DIR / 'audit.jsonl'


def _audit_log(command: str, args: dict[str, Any] | None = None) -> None:
    """Append a JSON-lines entry to ``~/.coinjure/audit.jsonl``."""
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        'timestamp': time.time(),
        'command': command,
        'args': args or {},
    }
    with open(_AUDIT_FILE, 'a') as fh:
        fh.write(json.dumps(entry, default=str) + '\n')


# ── Helpers ────────────────────────────────────────────────────────────────────


def _load_registry() -> StrategyRegistry:
    return StrategyRegistry(REGISTRY_PATH)


def _emit_json(data: Any) -> None:
    click.echo(json.dumps(data, default=str))


def _coinjure_cmd() -> str:
    found = shutil.which('coinjure')
    return found or sys.executable + ' -m coinjure.cli.cli'


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


def _registry_upsert(reg: StrategyRegistry, entry: StrategyEntry) -> None:
    """Add or update a registry entry (idempotent)."""
    try:
        reg.add(entry)
    except ValueError:
        reg.update(entry)


def _registry_prune_dead(reg: StrategyRegistry) -> int:
    """Remove registry entries whose PID is no longer alive. Return count removed."""
    import os

    removed = 0
    for e in reg.list():
        if e.lifecycle in ('retired',):
            continue
        pid = e.pid
        if not pid:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            # Process is dead — remove entry
            reg.remove(e.strategy_id)
            removed += 1
    return removed


def _broadcast_command(cmd: str, as_json: bool) -> None:
    """Send a control command to all active engine sockets."""
    reg = _load_registry()
    active = [e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')]
    results: list[dict] = []
    for entry in active:
        if not entry.socket_path:
            results.append(
                {'id': entry.strategy_id, 'ok': False, 'error': 'no_socket_path'}
            )
            continue
        try:
            resp = run_command(cmd, socket_path=Path(entry.socket_path))
            results.append({'id': entry.strategy_id, **resp})
        except Exception as exc:
            results.append({'id': entry.strategy_id, 'ok': False, 'error': str(exc)})
    payload = {'ok': True, 'count': len(results), 'results': results}
    if as_json:
        _emit_json(payload)
    else:
        for r in results:
            status = 'OK' if r.get('ok') else f'FAIL: {r.get("error")}'
            click.echo(f'  {r["id"]}: {status}')


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


def _run_batch(
    *,
    engine_cmd: str,
    relation_status: str,
    lifecycle: str,
    exchange: str,
    initial_capital: str,
    duration: float | None,
    as_json: bool,
    no_hub: bool = False,
    llm_portfolio_review: bool = False,
    llm_trade_sizing: bool = False,
    llm_model: str | None = None,
) -> None:
    """Launch all matching relations in a single multi-engine process.

    Instead of spawning N separate OS processes (one per relation), this
    writes a batch config file and spawns ONE subprocess running the
    internal ``_batch-run`` command, which uses :class:`MultiStrategyEngine` to
    run all strategies in a single event loop with shared market data.
    """
    from decimal import Decimal

    from coinjure.strategy.builtin import build_strategy_ref_for_relation
    from coinjure.trading.allocator import AllocationCandidate, allocate_capital

    relations = _load_relations_for_batch(relation_status)
    if not relations:
        raise click.ClickException(f'No relations with status {relation_status}.')

    if not no_hub:
        _ensure_hub_running(as_json=as_json)

    # ── Allocate capital across relations ──────────────────────────────
    candidates = [
        AllocationCandidate(
            strategy_id=rel.relation_id,
            backtest_pnl=rel.backtest_pnl or 0.0,
        )
        for rel in relations
    ]

    if llm_portfolio_review:
        from coinjure.trading.llm_allocator import allocate_capital_llm

        llm_kwargs: dict[str, Any] = {}
        if llm_model:
            llm_kwargs['model'] = llm_model
        budgets = asyncio.run(
            allocate_capital_llm(Decimal(initial_capital), candidates, **llm_kwargs)
        )
    else:
        budgets = allocate_capital(
            Decimal(initial_capital),
            candidates,
        )

    # ── Build slot configs ────────────────────────────────────────────
    slot_configs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for rel in relations:
        ref, kwargs = build_strategy_ref_for_relation(rel)
        if ref is None:
            skipped.append(
                {
                    'relation_id': rel.relation_id,
                    'ok': False,
                    'error': f'No strategy for spread_type: {rel.spread_type}',
                }
            )
            continue

        if llm_trade_sizing:
            kwargs['llm_trade_sizing'] = True
            if llm_model:
                kwargs['llm_model'] = llm_model
        if llm_portfolio_review:
            kwargs['llm_portfolio_review'] = True
            if llm_model:
                kwargs['llm_model'] = llm_model

        budget = budgets.get(rel.relation_id, Decimal('10'))
        slot_configs.append(
            {
                'relation_id': rel.relation_id,
                'strategy_ref': ref,
                'strategy_kwargs': kwargs,
                'budget': str(budget),
            }
        )

    if not slot_configs:
        raise click.ClickException('No valid strategies to run.')

    # ── Write batch config and spawn ONE process ──────────────────────
    import uuid

    config = {
        'exchange': exchange,
        'no_hub': no_hub,
        'duration': duration,
        'slot_configs': slot_configs,
    }
    config_path = SOCKET_DIR / f'batch-{uuid.uuid4().hex[:8]}.json'
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, default=str))

    cmd = shlex.split(_coinjure_cmd()) + [
        'engine',
        '_batch-run',
        '--config',
        str(config_path),
    ]
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    socket = str(SOCKET_DIR / f'engine-{proc.pid}.sock')

    # Register all strategies with the same PID / socket
    reg = _load_registry()
    results: list[dict[str, Any]] = list(skipped)
    for sc in slot_configs:
        entry = StrategyEntry(
            strategy_id=sc['relation_id'],
            strategy_ref=sc['strategy_ref'],
            strategy_kwargs=sc['strategy_kwargs'],
            relation_id=sc['relation_id'],
            lifecycle=lifecycle,
            exchange=exchange,
            pid=proc.pid,
            socket_path=socket,
        )
        _registry_upsert(reg, entry)
        results.append(
            {
                'relation_id': sc['relation_id'],
                'ok': True,
                'pid': proc.pid,
                'strategy_ref': sc['strategy_ref'],
                'socket': socket,
                'budget': sc['budget'],
            }
        )

    if as_json:
        _emit_json(
            {
                'ok': True,
                'mode': 'multi',
                'pid': proc.pid,
                'socket': socket,
                'launched': results,
                'count': len(slot_configs),
            }
        )
    else:
        click.echo(
            f'\nLaunched multi-engine (pid={proc.pid}) '
            f'with {len(slot_configs)} slots:\n'
        )
        for r in results:
            if r.get('ok'):
                click.echo(
                    f'  {r["relation_id"]}  '
                    f'budget=${r["budget"]}  {r["strategy_ref"]}'
                )
            else:
                click.echo(f'  {r["relation_id"]}  SKIPPED: {r["error"]}')


# ── Click group ────────────────────────────────────────────────────────────────


@click.group()
def engine() -> None:
    """Running engine instances — paper & live trading, management, batch ops."""


# ── _batch-run (internal) ──────────────────────────────────────────────────────


@engine.command('_batch-run', hidden=True)
@click.option('--config', 'config_path', required=True, type=click.Path(exists=True))
def engine_batch_run(config_path: str) -> None:
    """Internal: run multi-engine from a batch config file.

    Not intended for direct user invocation — called by ``_run_batch``
    inside a detached subprocess.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    config_data = json.loads(Path(config_path).read_text())
    exchange = config_data['exchange']
    no_hub = config_data.get('no_hub', False)
    duration = config_data.get('duration')
    slot_configs_raw = config_data['slot_configs']

    # Build data source (shared)
    hub_socket = HUB_SOCKET_PATH if (not no_hub and HUB_SOCKET_PATH.exists()) else None
    if hub_socket:
        from coinjure.data.live.polymarket import LiveRSSNewsDataSource
        from coinjure.data.source import CompositeDataSource
        from coinjure.hub.subscriber import HubDataSource

        # Collect all watch tokens from all strategies
        all_watch_tokens: list[str] = []
        for sc in slot_configs_raw:
            strategy_obj = _load_strategy(sc['strategy_ref'], sc['strategy_kwargs'])
            all_watch_tokens.extend(strategy_obj.watch_tokens())

        data_source = CompositeDataSource(
            [
                HubDataSource(hub_socket, tickers=all_watch_tokens),
                LiveRSSNewsDataSource(),
            ]
        )
    else:
        data_source = _build_market_source(exchange)

    from coinjure.engine.runner import SlotConfig, run_multi_paper_trading

    configs = [
        SlotConfig(
            slot_id=sc['relation_id'],
            strategy_ref=sc['strategy_ref'],
            strategy_kwargs=sc['strategy_kwargs'],
            initial_capital=Decimal(sc['budget']),
        )
        for sc in slot_configs_raw
    ]

    asyncio.run(
        run_multi_paper_trading(
            data_source=data_source,
            slot_configs=configs,
            duration=duration,
            continuous=True,
        )
    )

    # Clean up config file
    try:
        Path(config_path).unlink(missing_ok=True)
    except Exception:
        pass


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
@click.option(
    '--data-dir',
    default=None,
    type=click.Path(),
    help='Directory for persisting positions/trades across sessions (enables resume).',
)
@click.option(
    '--llm-portfolio-review',
    is_flag=True,
    default=False,
    help='Use LLM to review and adjust capital allocation across strategies.',
)
@click.option(
    '--llm-trade-sizing',
    is_flag=True,
    default=False,
    help='Use LLM to size individual trade opportunities at runtime (per-opportunity sizing; may call the LLM API for each trade).',
)
@click.option(
    '--llm-model',
    default=None,
    help='LLM model name for allocation/sizing (default: gpt-4.1-mini).',
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
    data_dir: str | None,
    llm_portfolio_review: bool,
    llm_trade_sizing: bool,
    llm_model: str | None,
) -> None:
    """Run a paper trading engine instance."""
    _audit_log(
        'paper-run',
        {
            'exchange': exchange,
            'duration': duration,
            'initial_capital': initial_capital,
            'strategy_ref': strategy_ref,
        },
    )
    if all_relations and strategy_ref:
        raise click.ClickException(
            '--all-relations and --strategy-ref are mutually exclusive.'
        )

    if all_relations:
        _run_batch(
            engine_cmd='paper-run',
            relation_status='backtest_passed',
            lifecycle='paper_trading',
            exchange='cross_platform',
            initial_capital=initial_capital,
            duration=duration,
            as_json=as_json,
            no_hub=no_hub,
            llm_portfolio_review=llm_portfolio_review,
            llm_trade_sizing=llm_trade_sizing,
            llm_model=llm_model,
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
        if data_dir:
            cmd += ['--data-dir', data_dir]
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
        _registry_upsert(reg, entry)

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

    from coinjure.engine.runner import run_live_paper_trading

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
            [
                HubDataSource(hub_socket, tickers=strategy_obj.watch_tokens()),
                LiveRSSNewsDataSource(),
            ]
        )
    else:
        data_source = _build_market_source(exchange)

    state_store = None
    if data_dir:
        from coinjure.storage.state_store import StateStore

        state_store = StateStore(data_dir)

    asyncio.run(
        run_live_paper_trading(
            data_source=data_source,
            strategy=strategy_obj,
            initial_capital=capital,
            duration=duration,
            state_store=state_store,
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
@click.option(
    '--llm-portfolio-review',
    is_flag=True,
    default=False,
    help='Use LLM to review and adjust capital allocation across strategies.',
)
@click.option(
    '--llm-trade-sizing',
    is_flag=True,
    default=False,
    help='Use LLM to size individual trade opportunities at runtime (per-opportunity sizing; may call the LLM API for each trade).',
)
@click.option(
    '--llm-model',
    default=None,
    help='LLM model name for allocation/sizing (default: gpt-4.1-mini).',
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
    llm_portfolio_review: bool,
    llm_trade_sizing: bool,
    llm_model: str | None,
) -> None:
    """Run a live trading engine instance (real orders, real funds)."""
    _audit_log(
        'live-run',
        {
            'exchange': exchange,
            'duration': duration,
            'initial_capital': initial_capital,
            'strategy_ref': strategy_ref,
        },
    )
    if all_relations and strategy_ref:
        raise click.ClickException(
            '--all-relations and --strategy-ref are mutually exclusive.'
        )

    _confirm_live_trading(as_json=as_json)

    if all_relations:
        _run_batch(
            engine_cmd='live-run',
            relation_status='deployed',
            lifecycle='live_trading',
            exchange=exchange,
            initial_capital=initial_capital,
            duration=duration,
            as_json=as_json,
            llm_portfolio_review=llm_portfolio_review,
            llm_trade_sizing=llm_trade_sizing,
            llm_model=llm_model,
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
        _registry_upsert(reg, entry)

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
    _registry_prune_dead(reg)
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
        # Pass strategy_id so MultiControlServer can route to the right slot
        extra: dict[str, Any] = {}
        if strategy_id:
            extra['strategy_id'] = strategy_id
        resp = run_command(cmd, socket_path=sock, **extra)
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
            portfolio = resp.get('portfolio') or {}
            realized = portfolio.get('realized_pnl')
            unrealized = portfolio.get('unrealized_pnl')
            pnl_str = ''
            if realized is not None:
                pnl_str = f' pnl={float(realized) + float(unrealized or 0):+.2f}'
            click.echo(
                'status={status} paused={paused} runtime={runtime} events={events} '
                'decisions={decisions} executed={executed} orders={orders}{pnl}'.format(
                    status='ok',
                    paused=resp.get('paused', False),
                    runtime=resp.get('runtime', '0:00:00'),
                    events=resp.get('event_count', 0),
                    decisions=resp.get('decisions', 0),
                    executed=resp.get('executed', 0),
                    orders=resp.get('orders', 0),
                    pnl=pnl_str,
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
            portfolio = resp.get('portfolio') or {}
            realized = portfolio.get('realized_pnl')
            unrealized = portfolio.get('unrealized_pnl')
            pnl_str = ''
            if realized is not None:
                pnl_str = f' pnl={float(realized) + float(unrealized or 0):+.2f}'
            click.echo(
                '{sid}  paused={paused} runtime={runtime} events={events} '
                'decisions={decisions} executed={executed} orders={orders}{pnl}'.format(
                    sid=entry.strategy_id,
                    paused=resp.get('paused', False),
                    runtime=resp.get('runtime', '0:00:00'),
                    events=resp.get('event_count', 0),
                    decisions=resp.get('decisions', 0),
                    executed=resp.get('executed', 0),
                    orders=resp.get('orders', 0),
                    pnl=pnl_str,
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
        _broadcast_command('pause', as_json)
        return
    sock = _resolve_socket_for_id(strategy_id, socket)
    extra: dict[str, Any] = {}
    if strategy_id:
        extra['strategy_id'] = strategy_id
    resp = run_command('pause', socket_path=sock, **extra)
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
    extra: dict[str, Any] = {}
    if strategy_id:
        extra['strategy_id'] = strategy_id
    resp = run_command('resume', socket_path=sock, **extra)
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
        _broadcast_command('stop', as_json)
        return
    sock = _resolve_socket_for_id(strategy_id, socket)
    extra: dict[str, Any] = {}
    if strategy_id:
        extra['strategy_id'] = strategy_id
    resp = run_command('stop', socket_path=sock, **extra)
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
    kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)

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
    retire_entry: StrategyEntry | None = reg.get(strategy_id)
    if retire_entry is None:
        raise click.ClickException(f'Strategy not found: {strategy_id!r}')

    stop_result_single: dict = {'ok': True, 'status': 'not_running'}

    if retire_entry.socket_path and Path(retire_entry.socket_path).exists():
        try:
            stop_result_single = run_command(
                'stop', socket_path=Path(retire_entry.socket_path)
            )
        except Exception as exc:
            stop_result_single = {'ok': False, 'error': str(exc)}

    retire_entry.lifecycle = 'retired'
    retire_entry.retired_reason = reason
    retire_entry.pid = None
    retire_entry.socket_path = None
    reg.update(retire_entry)

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
    from coinjure.engine.control import cleanup_stale_sockets

    # Remove stale sockets from dead engine processes before discovery
    removed = cleanup_stale_sockets()
    if removed:
        click.echo(f'Cleaned up {removed} stale socket(s).', err=True)

    if socket:
        socket_paths = [Path(socket)]
        socket_labels: dict = {}
        auto_discover = False
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
            found = sorted(SOCKET_DIR.glob('engine-*.sock'))
            if found:
                socket_paths = found
        auto_discover = True

    try:
        app = SocketTradingMonitorApp(
            socket_paths=socket_paths,
            socket_labels=socket_labels,
            auto_discover=auto_discover,
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
        # Give engines a moment to exit, then prune dead entries
        import time

        time.sleep(1.0)
        reg = _load_registry()
        pruned = _registry_prune_dead(reg)
        resp = {
            'ok': True,
            'status': 'enabled',
            'path': str(kill_file),
            'pruned': pruned,
        }
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
@click.option(
    '--llm-portfolio-review',
    is_flag=True,
    default=False,
    help='Use LLM to review and adjust capital allocation across strategies before backtest.',
)
@click.option(
    '--llm-trade-sizing',
    is_flag=True,
    default=False,
    help='Route trade sizing decisions through the LLM during backtest runtime (may trigger API calls during opportunity checks).',
)
@click.option(
    '--llm-model',
    default=None,
    help='LLM model name for allocation/sizing (default: gpt-4.1-mini). '
    'Supports any OpenAI-compatible model via OPENAI_BASE_URL.',
)
def engine_backtest(
    relation_ids: tuple[str, ...],
    all_relations: bool,
    rerun: bool,
    parquet_paths: tuple[str, ...],
    initial_capital: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    llm_portfolio_review: bool,
    llm_trade_sizing: bool,
    llm_model: str | None,
) -> None:
    """Backtest relations using auto-selected strategies.

    Each relation's spread_type determines the strategy. Results update the
    relation lifecycle (backtest_passed / backtest_failed) in the store.
    """
    _audit_log(
        'backtest',
        {
            'relation_ids': list(relation_ids),
            'all_relations': all_relations,
            'initial_capital': initial_capital,
        },
    )
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
            'llm_portfolio_review': llm_portfolio_review,
            'llm_trade_sizing': llm_trade_sizing,
            'llm_model': llm_model,
        },
        as_json=as_json,
    )

    # ── Optional LLM portfolio review ─────────────────────────────────
    llm_kwargs: dict[str, Any] = {}
    if llm_model:
        llm_kwargs['model'] = llm_model

    budgets: dict[str, Decimal] = {}
    if llm_portfolio_review:
        from coinjure.trading.allocator import AllocationCandidate
        from coinjure.trading.llm_allocator import allocate_capital_llm

        candidates = [
            AllocationCandidate(
                strategy_id=rel.relation_id,
                backtest_pnl=rel.backtest_pnl or 0.0,
            )
            for rel in relations
        ]
        if not as_json:
            click.echo('Running LLM portfolio allocation review...')
        budgets = asyncio.run(allocate_capital_llm(capital, candidates, **llm_kwargs))
        if not as_json:
            for sid, bgt in budgets.items():
                click.echo(f'  LLM budget: {sid[:30]:30s}  ${bgt:.2f}')

    if llm_trade_sizing and not as_json:
        click.echo('Using runtime LLM trade sizing during opportunity checks...')

    async def _run_all():
        results = []
        for rel in relations:
            merged_kwargs = dict(strategy_kwargs)
            if llm_trade_sizing:
                merged_kwargs['llm_trade_sizing'] = True
                if llm_model:
                    merged_kwargs['llm_model'] = llm_model
            if llm_portfolio_review:
                merged_kwargs['llm_portfolio_review'] = True
                if llm_model:
                    merged_kwargs['llm_model'] = llm_model

            rel_capital = budgets.get(rel.relation_id, capital) if budgets else capital

            result = await run_backtest_relation(
                rel,
                initial_capital=rel_capital,
                parquet_path=parquet,
                strategy_kwargs=merged_kwargs,
            )
            if result.error is None:
                rel.set_backtest_result(
                    passed=result.passed,
                    pnl=float(result.total_pnl),
                    trades=result.trade_count,
                )
                store.update(rel)
            results.append(result)
        return results

    from coinjure.engine.backtester import BacktestResult

    results: list[BacktestResult] = asyncio.run(_run_all())

    # Output results
    passed = sum(1 for br in results if br.passed)
    failed = sum(1 for br in results if not br.passed and br.error is None)
    errors = sum(1 for br in results if br.error is not None)

    if as_json:
        _emit_json(
            [
                {
                    'relation_id': br.relation_id,
                    'spread_type': br.spread_type,
                    'strategy': br.strategy_name,
                    'pnl': float(br.total_pnl),
                    'trades': br.trade_count,
                    'passed': br.passed,
                    'error': br.error,
                }
                for br in results
            ]
        )
    else:
        click.echo(
            f'\n  Backtest results: {passed} passed, {failed} failed, {errors} errors\n'
        )
        for br in results:
            status = 'PASS' if br.passed else ('ERROR' if br.error else 'FAIL')
            pnl_str = f'pnl={br.total_pnl:+.2f}' if br.error is None else br.error
            click.echo(
                f'  [{status}] {br.relation_id[:20]}  '
                f'{br.spread_type}  {br.strategy_name}  '
                f'{pnl_str}  trades={br.trade_count}'
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
            assert entry.relation_id  # filtered above: `and e.relation_id`
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

    # Single relation — relation_id is non-None (checked at line 1544)
    assert relation_id
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    rel.status = 'deployed'
    store.update(rel)

    promote_entry: StrategyEntry | None = reg.get(relation_id)
    if promote_entry:
        promote_entry.lifecycle = 'deployed'
        reg.update(promote_entry)

    if as_json:
        _emit_json({'ok': True, 'relation_id': relation_id, 'status': 'deployed'})
    else:
        click.echo(f'Promoted {relation_id} to deployed.')
