"""Strategy development & testing CLI group.

Commands
--------
  strategy validate    — validate strategy loads + dry-run
  strategy backtest    — single market backtest
  strategy batch       — 1 strategy x N markets
  strategy pipeline    — validate + backtest + stress + gate
  strategy gate        — standalone promotion gate check
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from coinjure.cli.agent_commands import (
    _build_mock_events,
    _IdleStrategy,
    _load_strategy,
    _load_strategy_class,
    _parse_strategy_kwargs_json,
)
from coinjure.cli.utils import _emit
from coinjure.events.events import OrderBookEvent, PriceChangeEvent
from coinjure.ticker.ticker import PolyMarketTicker


@click.group()
def strategy() -> None:
    """Strategy development & testing."""


# ── validate ──────────────────────────────────────────────────────────────────


@strategy.command('validate')
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
@click.option(
    '--dry-run',
    'do_dry_run',
    is_flag=True,
    default=False,
    help='Also feed mock events to confirm runtime behaviour.',
)
@click.option(
    '--events',
    default=8,
    show_default=True,
    type=click.IntRange(1, 50),
    help='Mock events to feed when --dry-run is set.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_validate(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    do_dry_run: bool,
    events: int,
    as_json: bool,
) -> None:
    """Validate that a strategy is importable, constructible, and (optionally) runtime-safe."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)

    payload: dict[str, Any] = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'strategy_kwargs': strategy_kwargs,
        'class': strategy_obj.__class__.__name__,
        'module': strategy_obj.__class__.__module__,
        'message': f'Valid strategy: {strategy_ref}',
    }

    if do_dry_run:
        from coinjure.data.market_data_manager import MarketDataManager
        from coinjure.position.position_manager import Position, PositionManager
        from coinjure.risk.risk_manager import NoRiskManager
        from coinjure.ticker.ticker import CashTicker
        from coinjure.trader.paper_trader import PaperTrader

        ticker = PolyMarketTicker(
            symbol='DRYRUN_YES',
            name='Dry Run Market',
            token_id='DRYRUN_YES',
            market_id='DRYRUN_MKT',
            event_id='DRYRUN_EVT',
            no_token_id='DRYRUN_NO',
        )
        market_data = MarketDataManager()
        position_manager = PositionManager()
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=Decimal('10000'),
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        trader = PaperTrader(
            market_data=market_data,
            risk_manager=NoRiskManager(),
            position_manager=position_manager,
            min_fill_rate=Decimal('1.0'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.0'),
        )
        event_stream = _build_mock_events(ticker, events)

        async def _run_stream() -> tuple[int, str]:
            processed = 0
            error_message = ''
            for event in event_stream:
                if isinstance(event, OrderBookEvent):
                    market_data.process_orderbook_event(event)
                elif isinstance(event, PriceChangeEvent):
                    market_data.process_price_change_event(event)
                try:
                    await strategy_obj.process_event(event, trader)
                    processed += 1
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    break
            return processed, error_message

        processed, error_message = asyncio.run(_run_stream())
        decision_stats = strategy_obj.get_decision_stats()
        decisions = strategy_obj.get_decisions()
        payload.update(
            {
                'ok': error_message == '',
                'events_requested': events,
                'events_processed': processed,
                'orders_created': len(trader.orders),
                'decision_stats': decision_stats,
                'decisions_sample': [
                    {
                        'timestamp': d.timestamp,
                        'ticker_name': d.ticker_name,
                        'action': d.action,
                        'executed': d.executed,
                        'confidence': d.confidence,
                        'reasoning': d.reasoning,
                        'signal_values': d.signal_values,
                    }
                    for d in decisions[-5:]
                ],
                'error': error_message or None,
                'message': 'Dry-run completed'
                if error_message == ''
                else 'Dry-run failed',
            }
        )
        _emit(payload, as_json=as_json)
        if error_message:
            raise click.ClickException(f'Dry-run failed: {error_message}')
        return

    _emit(payload, as_json=as_json)


# ── backtest ──────────────────────────────────────────────────────────────────


@strategy.command('backtest')
@click.option(
    '--history-file',
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help='JSONL history file (mutually exclusive with --parquet).',
)
@click.option(
    '--parquet',
    'parquet_path',
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help='Parquet orderbook snapshot file from pmxt archive.',
)
@click.option('--symbol', default='BACKTEST_TOKEN', show_default=True)
@click.option('--name', default='Backtest Market', show_default=True)
@click.option('--market-id', default=None)
@click.option('--event-id', default=None)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--spread',
    default='0.01',
    show_default=True,
    help='Synthetic bid-ask spread for simulated order book.',
)
@click.option(
    '--strategy-ref',
    default='coinjure.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--min-fill-rate', default='0.5', show_default=True)
@click.option('--max-fill-rate', default='1.0', show_default=True)
@click.option('--commission-rate', default='0.0', show_default=True)
@click.option(
    '--risk-profile',
    default='none',
    show_default=True,
    type=click.Choice(['none', 'standard']),
)
@click.option(
    '--all-markets-context/--primary-market-context',
    default=False,
    show_default=True,
    help='Expose all markets from the history file to the strategy context.',
)
@click.option(
    '--allow-cross-market-trading/--primary-market-only',
    default=False,
    show_default=True,
    help='Allow the strategy to place trades outside the requested market.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def strategy_backtest(
    history_file: str | None,
    parquet_path: str | None,
    symbol: str,
    name: str,
    market_id: str | None,
    event_id: str | None,
    initial_capital: str,
    spread: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    min_fill_rate: str,
    max_fill_rate: str,
    commission_rate: str,
    risk_profile: str,
    all_markets_context: bool,
    allow_cross_market_trading: bool,
    as_json: bool,
) -> None:
    """Run backtest mode with historical data + paper execution."""
    from coinjure.backtest.backtester import run_backtest, run_backtest_parquet

    if not history_file and not parquet_path:
        raise click.ClickException('Provide either --history-file or --parquet.')
    if history_file and parquet_path:
        raise click.ClickException(
            '--history-file and --parquet are mutually exclusive.'
        )

    if parquet_path:
        strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
        strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
        capital = Decimal(initial_capital)
        _emit(
            {
                'mode': 'backtest_parquet',
                'message': f'Starting parquet backtest: {strategy_ref}',
                'parquet_path': parquet_path,
                'market_id': market_id,
                'strategy_kwargs': strategy_kwargs,
            },
            as_json=as_json,
        )
        asyncio.run(
            run_backtest_parquet(
                parquet_path=parquet_path,
                initial_capital=capital,
                strategy=strategy_obj,
                market_id=market_id,
            )
        )
        _emit(
            {'mode': 'backtest_parquet', 'message': 'Parquet backtest completed'},
            as_json=as_json,
        )
        return

    if not market_id:
        raise click.ClickException('--market-id is required for history-file backtest.')
    if not event_id:
        raise click.ClickException('--event-id is required for history-file backtest.')

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    capital = Decimal(initial_capital)
    try:
        fill_min = Decimal(min_fill_rate)
        fill_max = Decimal(max_fill_rate)
        fee = Decimal(commission_rate)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            'Invalid fill/commission value. Use numeric decimals.'
        ) from exc
    if fill_min <= 0 or fill_max <= 0:
        raise click.ClickException('--min-fill-rate and --max-fill-rate must be > 0.')
    if fill_min > fill_max:
        raise click.ClickException('--min-fill-rate cannot exceed --max-fill-rate.')
    if fee < 0:
        raise click.ClickException('--commission-rate must be >= 0.')

    if as_json:
        from coinjure.cli.research_helpers import _run_backtest_once

        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
                min_fill_rate=fill_min,
                max_fill_rate=fill_max,
                commission_rate=fee,
                risk_profile=risk_profile,
                include_all_markets_context=all_markets_context,
                allow_cross_market_trading=allow_cross_market_trading,
            )
        except Exception as exc:  # noqa: BLE001
            _emit({'ok': False, 'error': str(exc)}, as_json=True)
            raise click.ClickException(str(exc)) from exc
        _emit({'ok': True, **metrics}, as_json=True)
        return

    if (
        fill_min != Decimal('0.5')
        or fill_max != Decimal('1.0')
        or fee != Decimal('0.0')
        or risk_profile != 'none'
    ):
        raise click.ClickException(
            'Custom fill/fee/risk options currently require --json mode.'
        )

    spread_val = Decimal(spread)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    no_symbol = f'{symbol}_NO'
    ticker = PolyMarketTicker(
        symbol=symbol,
        name=name,
        market_id=market_id,
        event_id=event_id,
        token_id=symbol,
        no_token_id=no_symbol,
    )
    _emit(
        {
            'mode': 'backtest',
            'message': f'Starting backtest: {strategy_ref}',
            'history_file': history_file,
            'symbol': symbol,
            'strategy_kwargs': strategy_kwargs,
        },
        as_json=as_json,
    )
    asyncio.run(
        run_backtest(
            history_file=history_file,
            ticker_symbol=ticker,
            initial_capital=capital,
            strategy=strategy_obj,
            spread=spread_val,
            include_all_markets_context=all_markets_context,
            allow_cross_market_trading=allow_cross_market_trading,
        )
    )
    _emit({'mode': 'backtest', 'message': 'Backtest completed'}, as_json=as_json)


# ── batch ─────────────────────────────────────────────────────────────────────


@strategy.command('batch')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--strategy-ref', required=True)
@click.option(
    '--strategy-kwargs-json',
    default='{}',
    show_default=True,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--limit', default=50, show_default=True, type=int, help='Max markets to test.'
)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def strategy_batch(
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    initial_capital: str,
    limit: int,
    output: str,
    as_json: bool,
) -> None:
    """Run one strategy across N markets and return per-market results + aggregate stats."""
    from statistics import mean, pstdev

    from coinjure.cli.research_helpers import (
        _parse_json_object,
        _run_backtest_once,
        _to_decimal,
        _to_float_metric,
        _write_jsonl,
    )

    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    seen: dict[tuple[str, str], None] = {}
    path = Path(history_file).expanduser().resolve()
    import json as _json

    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            mid = str(row.get('market_id', ''))
            eid = str(row.get('event_id', ''))
            if mid and eid:
                seen[(mid, eid)] = None
            if len(seen) >= limit:
                break
    pairs = list(seen.keys())[:limit]

    if not pairs:
        raise click.ClickException(
            'No valid (market_id, event_id) pairs found in history file.'
        )

    results: list[dict[str, object]] = []
    for market_id, event_id in pairs:
        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
            )
            results.append(
                {
                    'market_id': market_id,
                    'event_id': event_id,
                    'ok': True,
                    'metrics': metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    'market_id': market_id,
                    'event_id': event_id,
                    'ok': False,
                    'error': str(exc),
                }
            )

    _write_jsonl(output, results)

    ok_results = [r for r in results if r.get('ok')]
    win_rates = [
        _to_float_metric((r['metrics'] or {}).get('win_rate'))  # type: ignore[attr-defined]
        for r in ok_results
        if isinstance(r.get('metrics'), dict)
    ]
    sharpes = [
        _to_float_metric((r['metrics'] or {}).get('sharpe_ratio'))  # type: ignore[attr-defined]
        for r in ok_results
        if isinstance(r.get('metrics'), dict)
    ]
    pnls = [
        _to_float_metric((r['metrics'] or {}).get('total_pnl'))  # type: ignore[attr-defined]
        for r in ok_results
        if isinstance(r.get('metrics'), dict)
    ]
    win_rates_f = [v for v in win_rates if v is not None]
    sharpes_f = [v for v in sharpes if v is not None]
    pnls_f = [v for v in pnls if v is not None]
    pct_profitable = (
        (sum(1 for v in pnls_f if v > 0) / len(pnls_f) * 100) if pnls_f else 0.0
    )

    aggregate: dict[str, object] = {
        'mean_win_rate': str(round(mean(win_rates_f), 4)) if win_rates_f else None,
        'mean_sharpe': str(round(mean(sharpes_f), 4)) if sharpes_f else None,
        'stddev_sharpe': str(round(pstdev(sharpes_f), 4))
        if len(sharpes_f) > 1
        else None,
        'pct_profitable': str(round(pct_profitable, 1)),
        'mean_pnl': str(round(mean(pnls_f), 4)) if pnls_f else None,
    }

    payload = {
        'ok': True,
        'ok_markets': len(ok_results),
        'total_markets': len(results),
        'aggregate': aggregate,
        'output': str(Path(output).resolve()),
    }
    _emit(payload, as_json=as_json)


# ── pipeline ──────────────────────────────────────────────────────────────────


@strategy.command('pipeline')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--market-id', default=None)
@click.option('--event-id', default=None)
@click.option(
    '--market-sort-by',
    default='volatility',
    show_default=True,
    type=click.Choice(['file', 'points', 'volume', 'span', 'volatility', 'trend']),
)
@click.option(
    '--market-rank',
    default=1,
    show_default=True,
    type=int,
    help='When market/event are omitted, pick Nth market from ranked dataset.',
)
@click.option('--dry-run-events', default=10, show_default=True, type=int)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--spread',
    default='0.01',
    show_default=True,
    help='Synthetic bid/ask half-spread for the backtest MarketDataManager.',
)
@click.option('--min-trades', default=1, show_default=True, type=int)
@click.option('--min-total-pnl', default='0', show_default=True)
@click.option('--max-drawdown-pct', default='0.30', show_default=True)
@click.option('--batch-limit', default=20, show_default=True, type=int)
@click.option('--run-batch-markets/--no-run-batch-markets', default=True)
@click.option(
    '--skip-batch-if-gate-fails/--no-skip-batch-if-gate-fails',
    default=True,
    show_default=True,
    help='Skip batch-markets and stress tests when the primary backtest has 0 trades or gate fails.',
)
@click.option(
    '--artifacts-dir',
    default='data/research/alpha_pipeline',
    show_default=True,
    type=click.Path(file_okay=False),
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def strategy_pipeline(  # noqa: C901
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    market_id: str | None,
    event_id: str | None,
    market_sort_by: str,
    market_rank: int,
    dry_run_events: int,
    initial_capital: str,
    spread: str,
    min_trades: int,
    min_total_pnl: str,
    max_drawdown_pct: str,
    batch_limit: int,
    run_batch_markets: bool,
    skip_batch_if_gate_fails: bool,
    artifacts_dir: str,
    as_json: bool,
) -> None:
    """Run validate + backtest + stress + gate (+ optional batch) in one command."""
    import hashlib
    import logging
    from datetime import datetime, timezone

    from coinjure.cli.research_helpers import (
        _STRESS_SCENARIOS,
        _collect_market_summaries,
        _parse_json_object,
        _run_backtest_once,
        _run_strategy_dry_run,
        _sort_market_summaries,
        _to_decimal,
        _write_json,
        _write_jsonl,
    )

    _logger = logging.getLogger(__name__)

    if dry_run_events <= 0:
        raise click.ClickException('--dry-run-events must be > 0')
    if market_rank <= 0:
        raise click.ClickException('--market-rank must be > 0')
    if batch_limit <= 0:
        raise click.ClickException('--batch-limit must be > 0')

    capital = _to_decimal(initial_capital)
    min_pnl = _to_decimal(min_total_pnl)
    max_dd = _to_decimal(max_drawdown_pct)
    spread_decimal = _to_decimal(spread)
    if capital is None or min_pnl is None or max_dd is None:
        raise click.ClickException('Invalid capital or gate threshold value.')
    if spread_decimal is None or spread_decimal < Decimal('0'):
        raise click.ClickException(f'Invalid --spread: {spread}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    summaries = _collect_market_summaries(history_file)
    ranked = _sort_market_summaries(summaries, sort_by=market_sort_by)
    if not ranked:
        raise click.ClickException('No valid markets found in history file.')

    selected_market = None
    if market_id and event_id:
        selected_market = {
            'market_id': market_id,
            'event_id': event_id,
            'source': 'manual',
        }
    elif not market_id and not event_id:
        idx = market_rank - 1
        if idx >= len(ranked):
            raise click.ClickException(
                f'--market-rank {market_rank} exceeds available markets ({len(ranked)}).'
            )
        selected = ranked[idx]
        selected_market = {
            'market_id': str(selected['market_id']),
            'event_id': str(selected['event_id']),
            'source': 'auto',
            'rank': str(market_rank),
            'sort_by': market_sort_by,
            'question': str(selected.get('question') or ''),
        }
    else:
        raise click.ClickException(
            'Pass both --market-id and --event-id, or omit both for auto selection.'
        )

    market_id = str(selected_market['market_id'])
    event_id = str(selected_market['event_id'])

    out_dir = Path(artifacts_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    preflight = _run_strategy_dry_run(
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        initial_capital=capital,
        dry_run_events=dry_run_events,
    )
    preflight_path = out_dir / 'preflight.json'
    _write_json(str(preflight_path), preflight)
    if not preflight.get('ok'):
        payload: dict[str, Any] = {
            'passed': False,
            'message': 'Alpha pipeline failed preflight',
            'selected_market': selected_market,
            'preflight_file': str(preflight_path),
            'artifacts_dir': str(out_dir),
        }
        _emit(payload, as_json=as_json)
        raise click.ClickException('Alpha pipeline failed preflight.')

    metrics = _run_backtest_once(
        history_file=history_file,
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        market_id=market_id,
        event_id=event_id,
        initial_capital=capital,
        spread=spread_decimal,
    )
    single_path = out_dir / 'backtest_single.json'
    _write_json(str(single_path), metrics)

    primary_trades = int(metrics.get('total_trades', 0))  # type: ignore[call-overload]
    skip_heavy = skip_batch_if_gate_fails and primary_trades == 0

    stress_rows: list[dict[str, object]] = []
    for scenario in [] if skip_heavy else _STRESS_SCENARIOS:
        try:
            scenario_metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
                min_fill_rate=scenario['min_fill_rate'],  # type: ignore[arg-type]
                max_fill_rate=scenario['max_fill_rate'],  # type: ignore[arg-type]
                commission_rate=scenario['commission_rate'],  # type: ignore[arg-type]
                spread=spread_decimal,
            )
            stress_rows.append(
                {
                    'scenario': scenario['name'],
                    'ok': True,
                    'metrics': scenario_metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            stress_rows.append(
                {'scenario': scenario['name'], 'ok': False, 'error': str(exc)}
            )
    stress_path = out_dir / 'stress.jsonl'
    _write_jsonl(str(stress_path), stress_rows)

    trades = int(metrics.get('total_trades', 0))  # type: ignore[call-overload]
    pnl = _to_decimal(metrics.get('total_pnl'))
    dd = _to_decimal(metrics.get('max_drawdown'))
    gate_checks = {
        'dry_run_ok': bool(preflight.get('ok')),
        'min_trades_ok': trades >= min_trades,
        'min_pnl_ok': pnl is not None and pnl >= min_pnl,
        'max_drawdown_ok': dd is not None and dd <= max_dd,
    }
    gate_passed = all(gate_checks.values())
    gate_payload = {
        'passed': gate_passed,
        'checks': gate_checks,
        'metrics': metrics,
        'thresholds': {
            'min_trades': min_trades,
            'min_total_pnl': str(min_pnl),
            'max_drawdown_pct': str(max_dd),
        },
    }
    gate_path = out_dir / 'gate.json'
    _write_json(str(gate_path), gate_payload)

    batch_summary: dict[str, object] | None = None
    if run_batch_markets and not skip_heavy:
        ranked_batch = ranked[:batch_limit]
        batch_rows: list[dict[str, object]] = []
        for candidate in ranked_batch:
            candidate_market_id = str(candidate['market_id'])
            candidate_event_id = str(candidate['event_id'])
            try:
                candidate_metrics = _run_backtest_once(
                    history_file=history_file,
                    strategy_ref=strategy_ref,
                    strategy_kwargs=strategy_kwargs,
                    market_id=candidate_market_id,
                    event_id=candidate_event_id,
                    initial_capital=capital,
                    spread=spread_decimal,
                )
                batch_rows.append(
                    {
                        'market_id': candidate_market_id,
                        'event_id': candidate_event_id,
                        'ok': True,
                        'metrics': candidate_metrics,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                batch_rows.append(
                    {
                        'market_id': candidate_market_id,
                        'event_id': candidate_event_id,
                        'ok': False,
                        'error': str(exc),
                    }
                )
        batch_path = out_dir / 'batch_markets.jsonl'
        _write_jsonl(str(batch_path), batch_rows)
        ok_rows = [r for r in batch_rows if r.get('ok')]
        batch_summary = {
            'total_markets': len(batch_rows),
            'ok_markets': len(ok_rows),
            'output': str(batch_path),
        }

    payload = {
        'passed': gate_passed,
        'message': 'Alpha pipeline complete'
        if gate_passed
        else 'Alpha pipeline gate failed',
        'selected_market': selected_market,
        'artifacts_dir': str(out_dir),
        'files': {
            'preflight': str(preflight_path),
            'backtest_single': str(single_path),
            'stress': str(stress_path),
            'gate': str(gate_path),
        },
        'batch_markets': batch_summary,
        'metrics': metrics,
    }

    # Auto-record to experiment ledger
    try:
        from coinjure.research.ledger import ExperimentLedger, LedgerEntry

        run_id = hashlib.sha256(
            f'{strategy_ref}:{strategy_kwargs}:{market_id}:{event_id}:{datetime.now(timezone.utc).isoformat()}'.encode()
        ).hexdigest()[:12]

        ledger_entry = LedgerEntry(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_ref=strategy_ref,
            strategy_kwargs=strategy_kwargs,
            market_id=market_id,
            event_id=event_id,
            history_file=history_file,
            gate_passed=gate_passed,
            metrics=metrics,
            artifacts_dir=str(out_dir),
        )
        ExperimentLedger().append(ledger_entry)
    except Exception:  # noqa: BLE001
        _logger.warning('Failed to auto-record to experiment ledger', exc_info=True)

    _emit(payload, as_json=as_json)
    if not gate_passed:
        raise click.ClickException('Alpha pipeline gate failed.')


# ── gate ──────────────────────────────────────────────────────────────────────


@strategy.command('gate')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--spread', default='0.01', show_default=True)
@click.option('--min-trades', default=1, show_default=True, type=int)
@click.option('--min-total-pnl', default='0', show_default=True)
@click.option('--max-drawdown-pct', default='0.30', show_default=True)
@click.option('--json', 'as_json', is_flag=True, default=False)
def strategy_gate(
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    market_id: str,
    event_id: str,
    initial_capital: str,
    spread: str,
    min_trades: int,
    min_total_pnl: str,
    max_drawdown_pct: str,
    as_json: bool,
) -> None:
    """Standalone promotion gate check — backtest + threshold validation."""
    from coinjure.cli.research_helpers import (
        _parse_json_object,
        _run_backtest_once,
        _to_decimal,
    )

    capital = _to_decimal(initial_capital)
    min_pnl = _to_decimal(min_total_pnl)
    max_dd = _to_decimal(max_drawdown_pct)
    spread_decimal = _to_decimal(spread)
    if capital is None or min_pnl is None or max_dd is None:
        raise click.ClickException('Invalid capital or gate threshold value.')
    if spread_decimal is None or spread_decimal < Decimal('0'):
        raise click.ClickException(f'Invalid --spread: {spread}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    metrics = _run_backtest_once(
        history_file=history_file,
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        market_id=market_id,
        event_id=event_id,
        initial_capital=capital,
        spread=spread_decimal,
    )

    trades = int(metrics.get('total_trades', 0))  # type: ignore[call-overload]
    pnl = _to_decimal(metrics.get('total_pnl'))
    dd = _to_decimal(metrics.get('max_drawdown'))
    gate_checks = {
        'min_trades_ok': trades >= min_trades,
        'min_pnl_ok': pnl is not None and pnl >= min_pnl,
        'max_drawdown_ok': dd is not None and dd <= max_dd,
    }
    gate_passed = all(gate_checks.values())

    payload = {
        'passed': gate_passed,
        'checks': gate_checks,
        'metrics': metrics,
        'thresholds': {
            'min_trades': min_trades,
            'min_total_pnl': str(min_pnl),
            'max_drawdown_pct': str(max_dd),
        },
    }
    _emit(payload, as_json=as_json)
    if not gate_passed:
        raise click.ClickException('Gate check failed.')
