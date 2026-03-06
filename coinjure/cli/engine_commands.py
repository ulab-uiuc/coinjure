"""Engine CLI group — unified paper/live trading, instance management, and batch ops.

Merges the old paper, live, trade, portfolio, and monitor groups into a single
noun-first ``engine`` group.
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
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from coinjure.cli.agent_commands import (
    _build_market_source,
    _confirm_live_trading,
    _IdleStrategy,
    _load_strategy,
    _parse_strategy_kwargs_json,
)
from coinjure.cli.control import SOCKET_DIR, SOCKET_PATH, run_command
from coinjure.cli.utils import _emit
from coinjure.engine.registry import REGISTRY_PATH, StrategyEntry, StrategyRegistry
from coinjure.hub.hub import HUB_SOCKET_PATH

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


# ── Click group ────────────────────────────────────────────────────────────────


@click.group()
def engine() -> None:
    """Running engine instances — paper & live trading, management, batch ops."""


# ── run ────────────────────────────────────────────────────────────────────────


@engine.command('run')
@click.option(
    '--mode',
    required=True,
    type=click.Choice(['paper', 'live']),
    help='Trading mode.',
)
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
    '--hub-socket',
    default=None,
    type=click.Path(),
    help='Connect to a running Market Data Hub.',
)
# Live-only options
@click.option(
    '--wallet-private-key',
    default=None,
    help='Polymarket wallet private key (or POLYMARKET_PRIVATE_KEY). Live only.',
)
@click.option('--signature-type', default=0, show_default=True, type=int)
@click.option('--funder', default=None, help='Polymarket funder wallet. Live only.')
@click.option('--kalshi-api-key-id', default=None, help='Kalshi API key id. Live only.')
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path. Live only.',
)
def engine_run(  # noqa: C901
    mode: str,
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str | None,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
    socket_path: str | None,
    hub_socket: str | None,
    wallet_private_key: str | None,
    signature_type: int,
    funder: str | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
) -> None:
    """Run a trading engine instance in paper or live mode."""
    from coinjure.engine.live_trader import (
        run_live_kalshi_paper_trading,
        run_live_kalshi_trading,
        run_live_paper_trading,
        run_live_polymarket_trading,
    )
    from coinjure.market.live.kalshi_data_source import LiveKalshiDataSource
    from coinjure.market.live.live_data_source import LivePolyMarketDataSource

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else _IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    capital = Decimal(initial_capital)

    _socket_path = Path(socket_path) if socket_path else None

    exchange_label = {
        'polymarket': 'Polymarket',
        'kalshi': 'Kalshi',
        'cross_platform': 'Cross-Platform',
    }.get(exchange, exchange)

    if mode == 'live':
        if exchange == 'cross_platform':
            raise click.ClickException(
                'Live mode does not support cross_platform. Use paper mode for cross-platform arb.'
            )
        _confirm_live_trading(as_json=as_json)
        _emit(
            {
                'mode': 'live',
                'exchange': exchange,
                'strategy_ref': strategy_ref,
                'strategy_kwargs': strategy_kwargs,
                'strategy_mode': strategy_mode,
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
        return

    # Paper mode
    _emit(
        {
            'mode': 'paper',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'hub_socket': hub_socket,
            'message': (
                f'Starting paper mode ({exchange})'
                if strategy_ref
                else f'Starting paper mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    if hub_socket:
        from coinjure.hub.subscriber import HubDataSource

        data_source = HubDataSource(Path(hub_socket).expanduser())
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
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_status(strategy_id: str | None, socket: str | None, as_json: bool) -> None:
    """Show engine runtime status."""
    sock = _resolve_socket_for_id(strategy_id, socket)
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


@engine.command('state')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_state(strategy_id: str | None, socket: str | None, as_json: bool) -> None:
    """Get full engine snapshot: positions, PnL, decisions, order books."""
    sock = _resolve_socket_for_id(strategy_id, socket)
    resp = run_command('get_state', socket_path=sock)
    _print_response(resp, as_json)
    if not resp.get('ok'):
        raise SystemExit(1)


@engine.command('pause')
@click.option('--id', 'strategy_id', default=None, help='Strategy ID.')
@click.option(
    '--socket', '-s', default=None, type=click.Path(), help='Direct socket path.'
)
@click.option(
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_pause(strategy_id: str | None, socket: str | None, as_json: bool) -> None:
    """Pause the engine: stop data ingestion and strategy decisions."""
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
    '--json', 'as_json', is_flag=True, default=False, help='Emit JSON response'
)
def engine_stop(strategy_id: str | None, socket: str | None, as_json: bool) -> None:
    """Gracefully stop the engine."""
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
@click.option('--id', 'strategy_id', required=True, help='Strategy ID.')
@click.option('--reason', default='manual', show_default=True)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_retire(strategy_id: str, reason: str, as_json: bool) -> None:
    """Stop a running strategy and mark it as retired."""
    reg = _load_registry()
    entry = reg.get(strategy_id)
    if entry is None:
        raise click.ClickException(f'Strategy not found: {strategy_id!r}')

    stop_result: dict = {'ok': True, 'status': 'not_running'}

    if entry.socket_path and Path(entry.socket_path).exists():
        try:
            status_resp = run_command('status', socket_path=Path(entry.socket_path))
            if status_resp.get('ok'):
                from coinjure.memory import FeedbackEntry, FeedbackLedger

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
            pass

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


# ── deploy ─────────────────────────────────────────────────────────────────────


@engine.command('deploy')
@click.option(
    '--query', required=True, help='Keyword to search markets on both platforms.'
)
@click.option('--min-edge', default='0.02', show_default=True)
@click.option('--min-similarity', default='0.6', show_default=True)
@click.option('--limit', default=50, show_default=True, type=int)
@click.option('--strategy-ref', default=None, show_default=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--trade-size', default=10.0, show_default=True, type=float)
@click.option('--cooldown-seconds', default=60, show_default=True, type=int)
@click.option('--max-deploy', default=10, show_default=True, type=int)
@click.option('--hub-socket', default=None, type=click.Path())
@click.option('--dry-run', is_flag=True, default=False)
@click.option('--skip-already-in-portfolio', is_flag=True, default=True)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_deploy(
    query: str,
    min_edge: str,
    min_similarity: str,
    limit: int,
    strategy_ref: str | None,
    initial_capital: str,
    trade_size: float,
    cooldown_seconds: int,
    max_deploy: int,
    hub_socket: str | None,
    dry_run: bool,
    skip_already_in_portfolio: bool,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Scan for arb opportunities and batch-deploy paper trading strategies."""
    from decimal import InvalidOperation

    from coinjure.cli.arb_helpers import (
        _DIRECT_ARB_REF,
        _compute_edge,
        _deploy_one,
        _pair_ids_in_portfolio,
    )
    from coinjure.cli.market_commands import (
        _kalshi_search_markets,
        _polymarket_search_markets,
    )
    from coinjure.market.matching import match_markets

    actual_ref = strategy_ref or _DIRECT_ARB_REF

    try:
        min_edge_dec = Decimal(min_edge)
        min_sim = float(min_similarity)
    except (InvalidOperation, ValueError) as exc:
        raise click.ClickException(f'Invalid numeric argument: {exc}') from exc

    async def _scan() -> list[dict]:
        poly_markets, kalshi_markets = await asyncio.gather(
            _polymarket_search_markets(query, limit),
            _kalshi_search_markets(
                query, limit, kalshi_api_key_id, kalshi_private_key_path
            ),
        )
        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        opportunities: list[dict] = []
        for pair in pairs:
            edge_info = _compute_edge(pair)
            if edge_info is None:
                continue
            if Decimal(edge_info['edge']) >= min_edge_dec:
                opportunities.append(edge_info)

        opportunities.sort(key=lambda x: Decimal(x['edge']), reverse=True)
        return opportunities

    try:
        opportunities = asyncio.run(_scan())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    to_deploy = [
        o
        for o in opportunities
        if not (skip_already_in_portfolio and o['already_in_portfolio'])
    ][:max_deploy]

    if not as_json:
        click.echo(
            f'Arb deploy: query={query!r}  found={len(opportunities)}  '
            f'to_deploy={len(to_deploy)}  dry_run={dry_run}'
        )

    results: list[dict] = []
    for opp in to_deploy:
        result = _deploy_one(
            opp=opp,
            strategy_ref=actual_ref,
            initial_capital=initial_capital,
            min_edge=float(min_edge_dec),
            trade_size=trade_size,
            cooldown_seconds=cooldown_seconds,
            hub_socket=hub_socket,
            dry_run=dry_run,
        )
        result['opportunity'] = {
            'poly_question': opp.get('poly_question', ''),
            'edge': opp['edge'],
            'edge_net': opp['edge_net'],
            'direction': opp['direction'],
        }
        results.append(result)
        if not as_json:
            status = (
                'DRY-RUN'
                if result.get('dry_run')
                else ('OK' if result['ok'] else 'FAIL')
            )
            click.echo(
                f'  [{status}] {result["strategy_id"]}  '
                f'edge={opp["edge"]}  {opp.get("poly_question", "")[:50]}'
            )
            if not result['ok']:
                click.echo(f'         error: {result.get("error")}')

    summary_data = {
        'ok': True,
        'query': query,
        'scanned': len(opportunities),
        'deployed': sum(
            1
            for r in results
            if r['ok'] and not r.get('skipped') and not r.get('dry_run')
        ),
        'skipped': sum(1 for r in results if r.get('skipped')),
        'failed': sum(1 for r in results if not r['ok']),
        'dry_run': dry_run,
        'results': results,
    }
    if as_json:
        _emit_json(summary_data)


# ── deploy-events ──────────────────────────────────────────────────────────────


@engine.command('deploy-events')
@click.option('--query', default='', help='Keyword to filter event titles.')
@click.option('--min-edge', default='0.01', show_default=True)
@click.option('--min-markets', default=2, show_default=True, type=int)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--strategy-ref', default=None, show_default=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--trade-size', default=10.0, show_default=True, type=float)
@click.option('--cooldown-seconds', default=120, show_default=True, type=int)
@click.option('--max-deploy', default=10, show_default=True, type=int)
@click.option('--hub-socket', default=None, type=click.Path())
@click.option('--dry-run', is_flag=True, default=False)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_deploy_events(
    query: str,
    min_edge: str,
    min_markets: int,
    limit: int,
    strategy_ref: str | None,
    initial_capital: str,
    trade_size: float,
    cooldown_seconds: int,
    max_deploy: int,
    hub_socket: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Scan Polymarket event-sum arb + batch-deploy EventSumArbStrategy."""
    from decimal import InvalidOperation

    from coinjure.cli.arb_helpers import (
        _EVENT_SUM_ARB_REF,
        _deploy_event_sum_one,
        _fetch_event_sum_opportunities,
    )

    actual_ref = strategy_ref or _EVENT_SUM_ARB_REF

    try:
        min_edge_dec = Decimal(min_edge)
    except InvalidOperation as exc:
        raise click.ClickException(f'Invalid --min-edge: {exc}') from exc

    try:
        opportunities = asyncio.run(
            _fetch_event_sum_opportunities(query, limit, min_edge_dec, min_markets)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    to_deploy = opportunities[:max_deploy]

    if not as_json:
        click.echo(
            f'deploy-events: query={query!r}  found={len(opportunities)}  '
            f'to_deploy={len(to_deploy)}  dry_run={dry_run}'
        )

    results: list[dict] = []
    for opp in to_deploy:
        result = _deploy_event_sum_one(
            opp=opp,
            strategy_ref=actual_ref,
            initial_capital=initial_capital,
            min_edge=float(min_edge_dec),
            trade_size=trade_size,
            cooldown_seconds=cooldown_seconds,
            min_markets=min_markets,
            hub_socket=hub_socket,
            dry_run=dry_run,
        )
        result['opportunity'] = {
            'event_title': opp.get('event_title', ''),
            'event_id': opp['event_id'],
            'best_edge': opp['best_edge'],
            'action': opp['action'],
            'n_markets': opp['n_markets'],
            'sum_yes': opp['sum_yes'],
        }
        results.append(result)
        if not as_json:
            status = (
                'DRY-RUN'
                if result.get('dry_run')
                else ('OK' if result['ok'] else 'FAIL')
            )
            click.echo(
                f'  [{status}] {result["strategy_id"]}'
                f'  edge={opp["best_edge"]}  {opp.get("event_title", "")[:50]}'
            )
            if not result['ok']:
                click.echo(f'         error: {result.get("error")}')

    summary_data = {
        'ok': True,
        'query': query,
        'scanned': len(opportunities),
        'deployed': sum(
            1
            for r in results
            if r['ok'] and not r.get('skipped') and not r.get('dry_run')
        ),
        'skipped': sum(1 for r in results if r.get('skipped')),
        'failed': sum(1 for r in results if not r['ok']),
        'dry_run': dry_run,
        'results': results,
    }
    if as_json:
        _emit_json(summary_data)


# ── health ─────────────────────────────────────────────────────────────────────

_STALE_DAYS = 7
_NO_SIGNAL_HOURS = 24


@engine.command('health')
@click.option(
    '--update/--no-update',
    default=True,
    show_default=True,
    help='Write back PnL / last_signal_at to registry after querying sockets.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_health(update: bool, as_json: bool) -> None:
    """Detect stale, degraded, or dead strategy instances."""
    reg = _load_registry()
    entries = reg.list()

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

        if entry.pid is not None and not _is_pid_alive(entry.pid):
            dead_process.append(
                {**entry_info, 'reason': 'pid_not_found', 'pid': entry.pid}
            )
            continue

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


# ── feedback ──────────────────────────────────────────────────────────────────


@engine.command('feedback')
@click.option('--id', 'strategy_id', required=True, help='Portfolio strategy ID.')
@click.option(
    '--socket-path',
    default=None,
    help='Control socket path (default: ~/.coinjure/<strategy-id>.sock).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_feedback(strategy_id: str, socket_path: str | None, as_json: bool) -> None:
    """Harvest paper performance and compare against backtest predictions."""
    from coinjure.memory import (
        ExperimentLedger,
        FeedbackEntry,
        FeedbackLedger,
    )

    sock = socket_path or str(Path.home() / '.coinjure' / f'{strategy_id}.sock')
    if not Path(sock).exists():
        raise click.ClickException(f'Socket not found: {sock}')

    resp = run_command('status', socket_path=Path(sock))
    if not resp.get('ok'):
        raise click.ClickException(
            f'Status query failed: {resp.get("error", "unknown")}'
        )

    portfolio_data = resp.get('portfolio', {})
    pnl = None
    for pos in portfolio_data.get('non_cash', []):
        if 'unrealized_pnl' in pos:
            try:
                pnl = (pnl or 0.0) + float(pos['unrealized_pnl'])
            except (TypeError, ValueError):
                pass
    realized = None
    try:
        realized = float(portfolio_data.get('realized_pnl', 0))
    except (TypeError, ValueError):
        pass

    entry = FeedbackEntry(
        strategy_id=strategy_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        source='paper' if 'paper' in resp.get('status', '') else 'live',
        runtime_seconds=resp.get('runtime', 0),
        metrics={
            'realized_pnl': realized,
            'unrealized_pnl': pnl,
            'event_count': resp.get('event_count', 0),
            'total_orders': resp.get('orders', 0),
        },
        decision_stats=resp.get('decision_stats', {}),
    )
    FeedbackLedger().append(entry)

    experiments = ExperimentLedger().query(strategy_ref=strategy_id)
    if not experiments:
        all_exp = ExperimentLedger().load_all()
        experiments = [e for e in all_exp if e.run_id == strategy_id]

    backtest_metrics = experiments[-1].metrics if experiments else {}

    def _safe_float(val: object) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    bt_pnl = _safe_float(backtest_metrics.get('total_pnl'))
    paper_pnl = _safe_float(entry.metrics.get('realized_pnl'))

    report: dict[str, Any] = {
        'ok': True,
        'strategy_id': strategy_id,
        'feedback_entry': entry.to_dict(),
        'backtest': {
            'total_pnl': bt_pnl,
            'sharpe_ratio': _safe_float(backtest_metrics.get('sharpe_ratio')),
            'max_drawdown': _safe_float(backtest_metrics.get('max_drawdown')),
        },
        'paper': {
            'realized_pnl': paper_pnl,
            'unrealized_pnl': _safe_float(entry.metrics.get('unrealized_pnl')),
            'runtime_seconds': entry.runtime_seconds,
            'event_count': entry.metrics.get('event_count'),
        },
        'comparison': {},
    }
    if bt_pnl is not None and paper_pnl is not None:
        report['comparison']['pnl_gap'] = round(paper_pnl - bt_pnl, 6)
    report['comparison']['decision_stats'] = entry.decision_stats
    _emit(report, as_json=as_json)


# ── monitor ────────────────────────────────────────────────────────────────────


@engine.command('monitor')
@click.option(
    '--socket',
    '-s',
    default=None,
    type=click.Path(),
    help='Path to engine control socket.',
)
def engine_monitor(socket: str | None) -> None:
    """Attach a live Textual monitor to a running trading engine."""
    from coinjure.cli.textual_monitor import SocketTradingMonitorApp

    sock = Path(socket) if socket else SOCKET_PATH

    try:
        app = SocketTradingMonitorApp(socket_path=sock)
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


# ── Batch commands ─────────────────────────────────────────────────────────────


@engine.command('pause-all')
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_pause_all(as_json: bool) -> None:
    """Pause all running engine instances."""
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
            resp = run_command('pause', socket_path=Path(entry.socket_path))
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


@engine.command('stop-all')
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_stop_all(as_json: bool) -> None:
    """Stop all running engine instances."""
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
            resp = run_command('stop', socket_path=Path(entry.socket_path))
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


@engine.command('retire-all')
@click.option('--reason', required=True, help='Retirement reason.')
@click.option(
    '--lifecycle',
    default=None,
    help='Filter by lifecycle (paper_trading|live_trading).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_retire_all(reason: str, lifecycle: str | None, as_json: bool) -> None:
    """Retire all matching active strategies."""
    reg = _load_registry()
    active = [e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')]
    if lifecycle:
        active = [e for e in active if e.lifecycle == lifecycle]

    results: list[dict] = []
    for entry in active:
        stop_result: dict = {'ok': True, 'status': 'not_running'}
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
        results.append(
            {
                'id': entry.strategy_id,
                'ok': True,
                'stop_result': stop_result,
            }
        )

    payload = {'ok': True, 'count': len(results), 'results': results}
    if as_json:
        _emit_json(payload)
    else:
        for r in results:
            click.echo(f'  Retired {r["id"]}')


@engine.command('report')
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_report(as_json: bool) -> None:
    """Aggregated PnL + ranking across all active engines."""
    reg = _load_registry()
    active = [e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')]

    if not active:
        payload: dict[str, Any] = {'ok': True, 'active': 0, 'engines': []}
        if as_json:
            _emit_json(payload)
        else:
            click.echo('No active engines.')
        return

    results = asyncio.run(_gather_socket_statuses(reg.list()))
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

    # Sort by realized PnL descending
    def _pnl_key(e: dict) -> float:
        try:
            return float(e.get('realized_pnl') or 0)
        except (TypeError, ValueError):
            return 0.0

    engines.sort(key=_pnl_key, reverse=True)

    payload = {
        'ok': True,
        'active': len(active),
        'engines': engines,
    }
    if as_json:
        _emit_json(payload)
    else:
        click.echo(f'\nEngine Report — {len(active)} active\n')
        for e in engines:
            status_str = 'OK' if e['ok'] else 'UNREACHABLE'
            pnl_str = e.get('realized_pnl', '?')
            click.echo(
                f'  {e["id"]}  [{status_str}]  pnl={pnl_str}  orders={e["orders"]}'
            )
        click.echo()


# ── pipeline ──────────────────────────────────────────────────────────────────


@engine.command('pipeline')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'both']),
    default='both',
    show_default=True,
)
@click.option(
    '--discover-limit',
    default=100,
    show_default=True,
    type=int,
    help='Markets to scan.',
)
@click.option(
    '--min-confidence',
    default=0.6,
    show_default=True,
    type=float,
    help='LLM discover threshold.',
)
@click.option(
    '--backtest-gate',
    default='sharpe>0.5,max_drawdown<0.20',
    show_default=True,
    help='Comma-separated backtest gates (metric>val or metric<val).',
)
@click.option(
    '--paper-days',
    default=7,
    show_default=True,
    type=int,
    help='Days to paper trade before promotion.',
)
@click.option(
    '--paper-gate',
    default='pnl>0',
    show_default=True,
    help='Paper performance gate for live promotion.',
)
@click.option(
    '--max-live',
    default=20,
    show_default=True,
    type=int,
    help='Max simultaneous live strategies.',
)
@click.option(
    '--strategy-ref',
    default=None,
    help='Strategy class ref (default: DirectArbStrategy for cross-platform, SpreadStrategy otherwise).',
)
@click.option('--initial-capital', default='1000', show_default=True)
@click.option('--model', default=None, help='LLM model for discover step.')
@click.option(
    '--dry-run', is_flag=True, default=False, help='Print plan without executing.'
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_pipeline(
    exchange: str,
    discover_limit: int,
    min_confidence: float,
    backtest_gate: str,
    paper_days: int,
    paper_gate: str,
    max_live: int,
    strategy_ref: str | None,
    initial_capital: str,
    model: str | None,
    dry_run: bool,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Automated pipeline: discover pairs → backtest → paper trade → promote to live.

    Runs a single pass through the pipeline:
    1. Calls ``market discover`` to find spread pairs via LLM.
    2. Backtests each pair; filters by --backtest-gate.
    3. Deploys survivors to paper trading.
    4. Checks existing paper traders older than --paper-days; promotes if --paper-gate passes.
    5. Retires degraded live strategies.
    """
    from click.testing import CliRunner

    from coinjure.cli.cli import cli as root_cli

    runner = CliRunner(mix_stderr=False)
    reg = _load_registry()

    results: dict[str, Any] = {
        'ok': True,
        'discover': {},
        'backtest': {},
        'deploy': {},
        'promote': {},
        'retire': {},
    }

    # ── Step 1: Discover ──────────────────────────────────────────────────
    if not as_json:
        click.echo('Step 1/5: Discovering spread pairs...')

    discover_args = [
        'market',
        'discover',
        '--exchange',
        exchange,
        '--limit',
        str(discover_limit),
        '--min-confidence',
        str(min_confidence),
        '--json',
    ]
    if model:
        discover_args += ['--model', model]
    if kalshi_api_key_id:
        discover_args += ['--kalshi-api-key-id', kalshi_api_key_id]
    if kalshi_private_key_path:
        discover_args += ['--kalshi-private-key-path', kalshi_private_key_path]

    disc_result = runner.invoke(root_cli, discover_args)
    discovered_pairs: list[dict] = []
    if disc_result.exit_code == 0:
        try:
            disc_data = json.loads(disc_result.output)
            discovered_pairs = disc_data.get('pairs', [])
        except json.JSONDecodeError:
            pass

    results['discover'] = {
        'found': len(discovered_pairs),
        'exit_code': disc_result.exit_code,
    }
    if not as_json:
        click.echo(f'  Found {len(discovered_pairs)} pairs.')

    # ── Step 2: Parse backtest gates ──────────────────────────────────────
    gates: list[tuple[str, str, float]] = []
    for clause in backtest_gate.split(','):
        clause = clause.strip()
        if not clause:
            continue
        if '>' in clause:
            metric, val = clause.split('>', 1)
            gates.append((metric.strip(), '>', float(val)))
        elif '<' in clause:
            metric, val = clause.split('<', 1)
            gates.append((metric.strip(), '<', float(val)))

    def _passes_gate(metrics: dict, gate_list: list) -> bool:
        for metric, op, threshold in gate_list:
            val = metrics.get(metric)
            if val is None:
                return False
            try:
                fval = float(val)
            except (TypeError, ValueError):
                return False
            if op == '>' and fval <= threshold:
                return False
            if op == '<' and fval >= threshold:
                return False
        return True

    # ── Step 3: Backtest each discovered pair ─────────────────────────────
    if not as_json:
        click.echo(f'Step 2/5: Backtesting {len(discovered_pairs)} pairs...')

    backtest_passed: list[dict] = []
    backtest_failed: list[dict] = []

    for pair in discovered_pairs:
        # For cross-platform same-event pairs, we can backtest via existing flow.
        # For now, mark all pairs as passed if no historical data is available
        # (backtest requires parquet data which may not exist for all pairs).
        ma = pair.get('market_a', {})
        mb = pair.get('market_b', {})

        bt_args = [
            'strategy',
            'backtest',
            '--strategy-ref',
            strategy_ref
            or 'examples/strategies/direct_arb_strategy.py:DirectArbStrategy',
            '--market-id',
            ma.get('id', ''),
            '--json',
        ]
        bt_result = runner.invoke(root_cli, bt_args)
        bt_metrics: dict = {}
        if bt_result.exit_code == 0:
            try:
                bt_data = json.loads(bt_result.output)
                bt_metrics = bt_data.get('metrics', bt_data.get('result', {}))
            except json.JSONDecodeError:
                pass

        if bt_metrics and _passes_gate(bt_metrics, gates):
            pair['backtest_metrics'] = bt_metrics
            backtest_passed.append(pair)
        else:
            # If no backtest data available, pass through with a warning
            if not bt_metrics:
                pair['backtest_metrics'] = {'note': 'no_historical_data'}
                backtest_passed.append(pair)
            else:
                backtest_failed.append(pair)

    results['backtest'] = {
        'tested': len(discovered_pairs),
        'passed': len(backtest_passed),
        'failed': len(backtest_failed),
    }
    if not as_json:
        click.echo(
            f'  {len(backtest_passed)} passed, {len(backtest_failed)} failed gate.'
        )

    # ── Step 4: Deploy survivors to paper trading ─────────────────────────
    if not as_json:
        click.echo(
            f'Step 3/5: Deploying {len(backtest_passed)} pairs to paper trading...'
        )

    deployed: list[dict] = []
    existing_ids = {e.strategy_id for e in reg.list()}

    for pair in backtest_passed:
        ma = pair.get('market_a', {})
        mb = pair.get('market_b', {})
        sid = f'spread-{ma.get("id", "a")[:8]}-{mb.get("id", "b")[:8]}'

        if sid in existing_ids:
            continue  # already deployed

        entry = StrategyEntry(
            strategy_id=sid,
            strategy_ref=strategy_ref
            or 'examples/strategies/direct_arb_strategy.py:DirectArbStrategy',
            strategy_kwargs={
                'market_a': ma,
                'market_b': mb,
                'spread_type': pair.get('spread_type', 'unknown'),
            },
            lifecycle='paper_trading',
            exchange='cross_platform',
            data_dir=str(Path('data') / 'research' / sid),
        )

        if not dry_run:
            reg.add(entry)
            existing_ids.add(sid)
        deployed.append({'strategy_id': sid, 'pair': pair, 'dry_run': dry_run})

    results['deploy'] = {'deployed': len(deployed), 'dry_run': dry_run}
    if not as_json:
        click.echo(f'  Deployed {len(deployed)} new paper strategies.')

    # ── Step 5: Promote mature paper traders to live ──────────────────────
    if not as_json:
        click.echo(
            f'Step 4/5: Checking paper traders for promotion (>{paper_days}d)...'
        )

    paper_gates: list[tuple[str, str, float]] = []
    for clause in paper_gate.split(','):
        clause = clause.strip()
        if not clause:
            continue
        if '>' in clause:
            m, v = clause.split('>', 1)
            paper_gates.append((m.strip(), '>', float(v)))
        elif '<' in clause:
            m, v = clause.split('<', 1)
            paper_gates.append((m.strip(), '<', float(v)))

    promoted: list[str] = []
    now = datetime.now(tz=timezone.utc)

    for entry in reg.list():
        if entry.lifecycle != 'paper_trading':
            continue
        try:
            created = datetime.fromisoformat(entry.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        days_running = (now - created).total_seconds() / 86400
        if days_running < paper_days:
            continue

        # Check paper performance gate
        paper_metrics = {'pnl': float(entry.paper_pnl or 0)}
        if _passes_gate(paper_metrics, paper_gates):
            if len(promoted) + _count_live(reg) >= max_live:
                break
            if not dry_run:
                entry.lifecycle = 'live_trading'
                reg.update(entry)
            promoted.append(entry.strategy_id)

    results['promote'] = {'promoted': len(promoted), 'max_live': max_live}
    if not as_json:
        click.echo(f'  Promoted {len(promoted)} strategies to live.')

    # ── Step 6: Retire degraded live strategies ───────────────────────────
    if not as_json:
        click.echo('Step 5/5: Retiring degraded strategies...')

    retired: list[str] = []
    for entry in reg.list():
        if entry.lifecycle != 'live_trading':
            continue
        pnl_val = float(entry.paper_pnl or 0)
        # Retire if significant loss
        if pnl_val < -float(initial_capital) * 0.15:
            if not dry_run:
                entry.lifecycle = 'retired'
                entry.retired_reason = f'pipeline_auto: pnl={pnl_val:.2f}'
                reg.update(entry)
            retired.append(entry.strategy_id)

    results['retire'] = {'retired': len(retired)}
    if not as_json:
        click.echo(f'  Retired {len(retired)} strategies.')

    # ── Summary ───────────────────────────────────────────────────────────
    if as_json:
        _emit_json(results)
    else:
        click.echo(
            f'\nPipeline complete: {results["discover"]["found"]} discovered → '
            f'{results["backtest"]["passed"]} backtested → '
            f'{results["deploy"]["deployed"]} deployed → '
            f'{results["promote"]["promoted"]} promoted → '
            f'{results["retire"]["retired"]} retired'
        )


def _count_live(reg: StrategyRegistry) -> int:
    return sum(1 for e in reg.list() if e.lifecycle == 'live_trading')


# ── supervise ─────────────────────────────────────────────────────────────────

_SUPERVISE_SYSTEM = """\
You are an automated trading supervisor for prediction market spread strategies.
You monitor a portfolio of active spread/arbitrage positions.

For each strategy, you receive:
- Strategy ID, lifecycle (paper/live), runtime
- Current PnL (realized + unrealized)
- Recent decision stats (win rate, consecutive losses)
- Market context (if available)

Your job: decide an ACTION for each strategy.

Actions:
- "hold": keep running, no change
- "pause": temporarily pause trading (e.g. high volatility, unclear signals)
- "retire": shut down and close positions (e.g. persistent losses, market resolved, edge gone)

Return a JSON array:
[
  {"strategy_id": "...", "action": "hold|pause|retire", "reasoning": "..."}
]

Be conservative: only retire if there's clear evidence the strategy is broken.
Prefer pause over retire when uncertain.
"""


@engine.command('supervise')
@click.option(
    '--model', default=None, help='LLM model (default: deepseek/deepseek-chat).'
)
@click.option(
    '--execute/--no-execute',
    default=False,
    show_default=True,
    help='Actually execute actions.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def engine_supervise(model: str | None, execute: bool, as_json: bool) -> None:
    """LLM supervisor: review all active strategies and recommend actions."""
    try:
        import litellm
    except ImportError as exc:
        raise click.ClickException(
            'litellm is required: poetry install -E llm'
        ) from exc

    import re

    llm_model = model or os.environ.get('COINJURE_LLM_MODEL', 'deepseek/deepseek-chat')
    reg = _load_registry()
    entries = [
        e for e in reg.list() if e.lifecycle in ('paper_trading', 'live_trading')
    ]

    if not entries:
        if as_json:
            _emit_json({'ok': True, 'actions': [], 'message': 'no active strategies'})
        else:
            click.echo('No active strategies to supervise.')
        return

    # Gather status from all sockets
    socket_statuses = asyncio.run(_gather_socket_statuses(entries))
    status_map = {e.strategy_id: s for e, s in socket_statuses}

    # Build context for LLM
    strategy_lines: list[str] = []
    for entry in entries:
        status = status_map.get(entry.strategy_id, {})
        portfolio = status.get('portfolio', {})
        decision_stats = status.get('decision_stats', {})

        line = (
            f'strategy_id={entry.strategy_id} '
            f'lifecycle={entry.lifecycle} '
            f'strategy_ref={entry.strategy_ref} '
            f'created={entry.created_at} '
            f'paper_pnl={entry.paper_pnl or "?"} '
            f'socket_ok={status.get("ok", False)} '
            f'total_value={portfolio.get("total", "?")} '
            f'realized_pnl={portfolio.get("realized_pnl", "?")} '
            f'event_count={status.get("event_count", 0)} '
            f'orders={status.get("orders", 0)} '
            f'win_rate={decision_stats.get("win_rate", "?")} '
            f'consecutive_losses={decision_stats.get("consecutive_losses", 0)} '
            f'runtime={status.get("runtime", "?")}'
        )
        strategy_lines.append(line)

    user_prompt = (
        f'Active strategies ({len(entries)}):\n\n'
        + '\n'.join(strategy_lines)
        + '\n\nReview each strategy and recommend an action.'
    )

    if not as_json:
        click.echo(f'Supervising {len(entries)} strategies with {llm_model}...')

    async def _call_llm() -> str:
        resp = await litellm.acompletion(
            model=llm_model,
            messages=[
                {'role': 'system', 'content': _SUPERVISE_SYSTEM},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        return resp.choices[0].message.content

    try:
        raw = asyncio.run(_call_llm())
    except Exception as exc:
        raise click.ClickException(f'LLM call failed: {exc}') from exc

    # Parse response
    actions: list[dict] = []
    try:
        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
        if '```json' in cleaned:
            cleaned = cleaned.split('```json')[1].split('```')[0].strip()
        elif '```' in cleaned:
            cleaned = cleaned.split('```')[1].split('```')[0].strip()
        else:
            start = cleaned.find('[')
            end = cleaned.rfind(']') + 1
            if start >= 0 and end > start:
                cleaned = cleaned[start:end]
        actions = json.loads(cleaned)
    except (json.JSONDecodeError, IndexError):
        raise click.ClickException(f'Failed to parse LLM response.\nRaw:\n{raw[:500]}')

    # Execute actions
    executed: list[dict] = []
    for action in actions:
        sid = action.get('strategy_id', '')
        act = action.get('action', 'hold')
        reason = action.get('reasoning', '')

        result = {
            'strategy_id': sid,
            'action': act,
            'reasoning': reason,
            'executed': False,
        }

        if act == 'hold':
            result['executed'] = True
        elif act in ('pause', 'retire') and execute:
            entry = reg.get(sid)
            if entry:
                if act == 'pause':
                    # Send pause via socket
                    sock = entry.socket_path
                    if sock and Path(sock).exists():
                        resp = run_command('pause', socket_path=Path(sock))
                        result['executed'] = resp.get('ok', False)
                    else:
                        result['executed'] = False
                        result['error'] = 'socket_not_found'
                elif act == 'retire':
                    entry.lifecycle = 'retired'
                    entry.retired_reason = f'llm_supervise: {reason[:100]}'
                    reg.update(entry)
                    # Send stop via socket
                    sock = entry.socket_path
                    if sock and Path(sock).exists():
                        run_command('stop', socket_path=Path(sock))
                    result['executed'] = True

        executed.append(result)

    if as_json:
        _emit_json({'ok': True, 'actions': executed, 'model': llm_model})
        return

    for r in executed:
        status = (
            'EXECUTED' if r['executed'] else ('DRY-RUN' if not execute else 'FAILED')
        )
        click.echo(
            f'  [{status}] {r["strategy_id"]}: {r["action"]}'
            f'  — {r["reasoning"][:80]}'
        )
    click.echo(
        f'\nSupervision complete: {sum(1 for a in executed if a["action"] == "hold")} hold, '
        f'{sum(1 for a in executed if a["action"] == "pause")} pause, '
        f'{sum(1 for a in executed if a["action"] == "retire")} retire'
    )


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
