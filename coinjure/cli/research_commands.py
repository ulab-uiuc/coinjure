"""Research tooling for strategy discovery and evaluation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import pstdev
from typing import Any

import click

from coinjure.core.trading_engine import TradingEngine
from coinjure.data.backtest.historical_data_source import HistoricalDataSource
from coinjure.data.market_data_manager import MarketDataManager
from coinjure.position.position_manager import Position, PositionManager
from coinjure.risk.risk_manager import NoRiskManager, StandardRiskManager
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import CashTicker, PolyMarketTicker
from coinjure.trader.paper_trader import PaperTrader


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, default=str))
        return
    if isinstance(payload, dict):
        click.echo(payload.get('message', str(payload)))
        return
    click.echo(str(payload))


def _parse_json_object(raw: str, *, option_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f'Invalid {option_name}: {exc.msg}') from exc
    if not isinstance(parsed, dict):
        raise click.ClickException(f'{option_name} must be a JSON object.')
    return parsed


def _to_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None


def _to_timestamp(value: object) -> int | None:
    """Convert a timestamp value into epoch seconds.

    Supports:
    - integer / numeric epoch timestamps
    - ISO-8601 strings like ``2025-12-06T06:00:14+00:00`` or ``...Z``
    """
    as_int = _to_int(value)
    if as_int is not None:
        return as_int

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.endswith('Z'):
                raw = f'{raw[:-1]}+00:00'
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            return None

    return None


def _series_to_rows(series: list[tuple[int, Decimal]]) -> list[dict[str, object]]:
    return [{'t': ts, 'p': str(price)} for ts, price in series]


def _load_yes_series(  # noqa: C901
    history_file: str,
    market_id: str,
    event_id: str,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    max_points: int | None = None,
) -> list[tuple[int, Decimal]]:
    path = Path(history_file).expanduser().resolve()
    if not path.exists():
        raise click.ClickException(f'History file not found: {path}')

    raw_points: list[tuple[int, Decimal]] = []
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('event_id') != event_id or row.get('market_id') != market_id:
                continue
            yes_series = (row.get('time_series') or {}).get('Yes')
            if not isinstance(yes_series, list):
                continue
            for point in yes_series:
                if not isinstance(point, dict):
                    continue
                ts = _to_timestamp(point.get('t'))
                price = _to_decimal(point.get('p'))
                if ts is None or price is None:
                    continue
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                raw_points.append((ts, price))

    if not raw_points:
        return []

    dedup: dict[int, Decimal] = {}
    for ts, price in raw_points:
        dedup[ts] = price

    series = sorted(dedup.items(), key=lambda x: x[0])
    if max_points and max_points > 0 and len(series) > max_points:
        series = series[-max_points:]
    return series


def _write_series_history_file(
    output_file: str,
    market_id: str,
    event_id: str,
    series: list[tuple[int, Decimal]],
) -> int:
    out_path = Path(output_file).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open('w', encoding='utf-8') as f:
        for ts, price in series:
            row = {
                'event_id': event_id,
                'market_id': market_id,
                'time_series': {'Yes': [{'t': ts, 'p': float(price)}]},
            }
            f.write(json.dumps(row) + '\n')
            count += 1
    return count


def _parse_windows(windows: str) -> list[int]:
    vals: list[int] = []
    for token in windows.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            val = int(token)
        except ValueError as exc:
            raise click.ClickException(f'Invalid window value: {token}') from exc
        if val <= 0:
            raise click.ClickException(f'Window must be > 0: {val}')
        vals.append(val)
    if not vals:
        raise click.ClickException('At least one valid window is required.')
    return sorted(set(vals))


def _rolling_mean(prices: list[Decimal], idx: int, window: int) -> Decimal | None:
    if idx + 1 < window:
        return None
    chunk = prices[idx + 1 - window : idx + 1]
    return sum(chunk, Decimal('0')) / Decimal(window)


def _rolling_zscore(prices: list[Decimal], idx: int, window: int) -> Decimal | None:
    if idx + 1 < window:
        return None
    chunk = prices[idx + 1 - window : idx + 1]
    vals = [float(p) for p in chunk]
    sigma = pstdev(vals)
    if sigma == 0:
        return Decimal('0')
    mu = sum(vals) / len(vals)
    return Decimal(str((float(prices[idx]) - mu) / sigma))


def _build_feature_rows(
    series: list[tuple[int, Decimal]], *, windows: list[int], z_window: int
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    prices = [p for _, p in series]
    for i, (ts, price) in enumerate(series):
        row: dict[str, object] = {'t': ts, 'price': str(price)}
        if i > 0 and prices[i - 1] > 0:
            row['ret_1'] = str((price / prices[i - 1]) - Decimal('1'))
        else:
            row['ret_1'] = None

        for window in windows:
            mom_key = f'momentum_{window}'
            sma_key = f'sma_{window}'
            if i >= window and prices[i - window] > 0:
                row[mom_key] = str((price / prices[i - window]) - Decimal('1'))
            else:
                row[mom_key] = None
            sma = _rolling_mean(prices, i, window)
            row[sma_key] = str(sma) if sma is not None else None

        zscore = _rolling_zscore(prices, i, z_window)
        row[f'zscore_{z_window}'] = str(zscore) if zscore is not None else None
        rows.append(row)
    return rows


def _build_label_rows(
    series: list[tuple[int, Decimal]],
    *,
    horizon_steps: int,
    threshold: Decimal,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if horizon_steps <= 0:
        raise click.ClickException('--horizon-steps must be > 0')
    last_idx = len(series) - horizon_steps
    if last_idx <= 0:
        return rows

    for i in range(last_idx):
        cur_ts, cur_price = series[i]
        fut_ts, fut_price = series[i + horizon_steps]
        if cur_price <= 0:
            continue
        future_return = (fut_price / cur_price) - Decimal('1')
        rows.append(
            {
                't': cur_ts,
                'future_t': fut_ts,
                'price': str(cur_price),
                'future_price': str(fut_price),
                'future_return': str(future_return),
                'label_up': future_return >= threshold,
                'label_down': future_return <= -threshold,
            }
        )
    return rows


def _write_jsonl(output_file: str, rows: list[dict[str, object]]) -> int:
    out = Path(output_file).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    return len(rows)


def _strategy_from_ref(strategy_ref: str, strategy_kwargs: dict[str, Any]) -> Strategy:
    from coinjure.cli.agent_commands import _load_strategy

    return _load_strategy(strategy_ref, strategy_kwargs)


def _run_backtest_once(
    *,
    history_file: str,
    strategy_ref: str,
    strategy_kwargs: dict[str, Any],
    market_id: str,
    event_id: str,
    initial_capital: Decimal,
    min_fill_rate: Decimal = Decimal('0.5'),
    max_fill_rate: Decimal = Decimal('1.0'),
    commission_rate: Decimal = Decimal('0.0'),
    risk_profile: str = 'none',
) -> dict[str, object]:
    strategy = _strategy_from_ref(strategy_ref, strategy_kwargs)
    ticker = PolyMarketTicker(
        symbol='RESEARCH_TOKEN',
        name='Research Market',
        market_id=market_id,
        event_id=event_id,
        token_id='RESEARCH_TOKEN',
    )

    async def _run() -> dict[str, object]:
        data_source = HistoricalDataSource(history_file, ticker)
        market_data = MarketDataManager()
        position_manager = PositionManager()
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=initial_capital,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        if risk_profile == 'standard':
            risk_manager = StandardRiskManager(
                position_manager=position_manager,
                market_data=market_data,
                initial_capital=initial_capital,
            )
        else:
            risk_manager = NoRiskManager()

        trader = PaperTrader(
            market_data=market_data,
            risk_manager=risk_manager,
            position_manager=position_manager,
            min_fill_rate=min_fill_rate,
            max_fill_rate=max_fill_rate,
            commission_rate=commission_rate,
        )
        engine = TradingEngine(
            data_source=data_source,
            strategy=strategy,
            trader=trader,
            initial_capital=initial_capital,
        )
        await engine.start()
        stats = engine._perf.get_stats()
        decision_stats = strategy.get_decision_stats()
        return {
            'total_trades': stats.total_trades,
            'winning_trades': stats.winning_trades,
            'losing_trades': stats.losing_trades,
            'win_rate': str(stats.win_rate),
            'total_pnl': str(stats.total_pnl),
            'average_profit': str(stats.average_profit),
            'average_loss': str(stats.average_loss),
            'profit_factor': str(stats.profit_factor),
            'max_drawdown': str(stats.max_drawdown),
            'sharpe_ratio': str(stats.sharpe_ratio),
            'decision_stats': decision_stats,
            'orders': len(trader.orders),
        }

    return asyncio.run(_run())


def _load_jsonl_rows(jsonl_file: str) -> list[dict[str, Any]]:
    path = Path(jsonl_file).expanduser().resolve()
    if not path.exists():
        raise click.ClickException(f'JSONL file not found: {path}')
    rows: list[dict[str, Any]] = []
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _to_float_metric(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return None


@click.group()
def research() -> None:
    """Research and strategy-discovery tooling."""


@research.command('universe')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--limit', default=100, show_default=True, type=int)
@click.option('--min-volume', default=0.0, show_default=True, type=float)
@click.option('--max-spread', default=None, type=float)
@click.option('--kalshi-api-key-id', default=None)
@click.option('--kalshi-private-key-path', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_universe(
    exchange: str,
    limit: int,
    min_volume: float,
    max_spread: float | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Build a tradable market universe with liquidity/spread filters."""
    from coinjure.cli.market_commands import (
        _kalshi_list_markets,
        _polymarket_list_markets,
    )

    if exchange == 'polymarket':
        markets = asyncio.run(_polymarket_list_markets(limit))
    else:
        markets = asyncio.run(
            _kalshi_list_markets(limit, kalshi_api_key_id, kalshi_private_key_path)
        )

    selected: list[dict[str, object]] = []
    for row in markets:
        if exchange == 'polymarket':
            bid = _to_float_metric(row.get('best_bid'))
            ask = _to_float_metric(row.get('best_ask'))
            volume = _to_float_metric(row.get('volume')) or 0.0
            spread = (ask - bid) if bid is not None and ask is not None else None
            key = str(row.get('id', ''))
            title = str(row.get('question', ''))
        else:
            bid = _to_float_metric(row.get('yes_bid'))
            ask = _to_float_metric(row.get('yes_ask'))
            if bid is not None:
                bid /= 100.0
            if ask is not None:
                ask /= 100.0
            volume = _to_float_metric(row.get('volume')) or 0.0
            spread = (ask - bid) if bid is not None and ask is not None else None
            key = str(row.get('ticker', ''))
            title = str(row.get('title', ''))

        if volume < min_volume:
            continue
        if max_spread is not None and spread is not None and spread > max_spread:
            continue
        selected.append(
            {
                'id': key,
                'title': title,
                'best_bid': bid,
                'best_ask': ask,
                'spread': spread,
                'volume': volume,
            }
        )
    selected.sort(key=lambda x: float(x.get('volume', 0.0)), reverse=True)
    payload = {'exchange': exchange, 'count': len(selected), 'markets': selected}
    _emit(payload, as_json=as_json)


@research.command('slice')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--start-ts', default=None, type=int)
@click.option('--end-ts', default=None, type=int)
@click.option('--max-points', default=None, type=int)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_slice(
    history_file: str,
    market_id: str,
    event_id: str,
    start_ts: int | None,
    end_ts: int | None,
    max_points: int | None,
    output: str,
    as_json: bool,
) -> None:
    """Slice yes/no time-series data by market/event/time range."""
    series = _load_yes_series(
        history_file,
        market_id,
        event_id,
        start_ts=start_ts,
        end_ts=end_ts,
        max_points=max_points,
    )
    count = _write_series_history_file(output, market_id, event_id, series)
    payload = {
        'message': 'Slice written',
        'history_file': str(Path(history_file).resolve()),
        'output': str(Path(output).resolve()),
        'points': count,
        'first_ts': series[0][0] if series else None,
        'last_ts': series[-1][0] if series else None,
    }
    _emit(payload, as_json=as_json)


@research.command('features')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--windows', default='3,5,10', show_default=True)
@click.option('--z-window', default=20, show_default=True, type=int)
@click.option('--start-ts', default=None, type=int)
@click.option('--end-ts', default=None, type=int)
@click.option('--max-points', default=None, type=int)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_features(
    history_file: str,
    market_id: str,
    event_id: str,
    windows: str,
    z_window: int,
    start_ts: int | None,
    end_ts: int | None,
    max_points: int | None,
    output: str,
    as_json: bool,
) -> None:
    """Build feature rows from yes/no price series."""
    if z_window <= 1:
        raise click.ClickException('--z-window must be > 1')
    win = _parse_windows(windows)
    series = _load_yes_series(
        history_file,
        market_id,
        event_id,
        start_ts=start_ts,
        end_ts=end_ts,
        max_points=max_points,
    )
    rows = _build_feature_rows(series, windows=win, z_window=z_window)
    count = _write_jsonl(output, rows)
    payload = {
        'message': 'Features built',
        'output': str(Path(output).resolve()),
        'rows': count,
        'windows': win,
        'z_window': z_window,
    }
    _emit(payload, as_json=as_json)


@research.command('labels')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--horizon-steps', default=5, show_default=True, type=int)
@click.option('--threshold', default='0.0', show_default=True)
@click.option('--start-ts', default=None, type=int)
@click.option('--end-ts', default=None, type=int)
@click.option('--max-points', default=None, type=int)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_labels(
    history_file: str,
    market_id: str,
    event_id: str,
    horizon_steps: int,
    threshold: str,
    start_ts: int | None,
    end_ts: int | None,
    max_points: int | None,
    output: str,
    as_json: bool,
) -> None:
    """Build forward-return labels from yes/no price series."""
    thr = _to_decimal(threshold)
    if thr is None:
        raise click.ClickException(f'Invalid threshold: {threshold}')
    series = _load_yes_series(
        history_file,
        market_id,
        event_id,
        start_ts=start_ts,
        end_ts=end_ts,
        max_points=max_points,
    )
    rows = _build_label_rows(series, horizon_steps=horizon_steps, threshold=thr)
    count = _write_jsonl(output, rows)
    payload = {
        'message': 'Labels built',
        'output': str(Path(output).resolve()),
        'rows': count,
        'horizon_steps': horizon_steps,
        'threshold': str(thr),
    }
    _emit(payload, as_json=as_json)


@research.command('backtest-batch')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option(
    '--params-jsonl', default=None, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--max-runs', default=None, type=int)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_backtest_batch(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    params_jsonl: str | None,
    initial_capital: str,
    max_runs: int | None,
    output: str,
    as_json: bool,
) -> None:
    """Run a batch of parameterized backtests."""
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')

    if params_jsonl:
        params_rows = _load_jsonl_rows(params_jsonl)
        if not params_rows:
            raise click.ClickException('No valid rows found in --params-jsonl')
    else:
        params_rows = [{}]

    if max_runs and max_runs > 0:
        params_rows = params_rows[:max_runs]

    results: list[dict[str, object]] = []
    for idx, row in enumerate(params_rows, start=1):
        strategy_kwargs = row.get('strategy_kwargs')
        if not isinstance(strategy_kwargs, dict):
            strategy_kwargs = {
                k: v for k, v in row.items() if k not in {'name', 'id', 'run_id'}
            }
        run_id = str(row.get('id') or row.get('run_id') or f'run-{idx}')
        run_name = str(row.get('name') or run_id)
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
                    'run_id': run_id,
                    'name': run_name,
                    'strategy_kwargs': strategy_kwargs,
                    'ok': True,
                    'metrics': metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    'run_id': run_id,
                    'name': run_name,
                    'strategy_kwargs': strategy_kwargs,
                    'ok': False,
                    'error': str(exc),
                }
            )

    _write_jsonl(output, results)
    payload = {
        'message': 'Batch backtest complete',
        'output': str(Path(output).resolve()),
        'runs': len(results),
        'ok_runs': sum(1 for r in results if r.get('ok')),
    }
    _emit(payload, as_json=as_json)


@research.command('walk-forward')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--train-size', default=300, show_default=True, type=int)
@click.option('--test-size', default=120, show_default=True, type=int)
@click.option('--step-size', default=120, show_default=True, type=int)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_walk_forward(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    train_size: int,
    test_size: int,
    step_size: int,
    initial_capital: str,
    output: str,
    as_json: bool,
) -> None:
    """Run walk-forward evaluation on yes/no time series."""
    if min(train_size, test_size, step_size) <= 0:
        raise click.ClickException('train/test/step sizes must all be > 0')
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    series = _load_yes_series(history_file, market_id, event_id)
    if len(series) < train_size + test_size:
        raise click.ClickException('Not enough points for the requested walk-forward.')

    rows: list[dict[str, object]] = []
    offset = 0
    run_idx = 1
    while offset + train_size + test_size <= len(series):
        train_start = offset
        train_end = offset + train_size
        test_end = train_end + test_size
        test_series = series[train_end:test_end]
        tmp_file = tempfile.NamedTemporaryFile(
            prefix='coinjure_walk_forward_',
            suffix='.jsonl',
            delete=False,
        )
        tmp_file.close()
        tmp_path = tmp_file.name
        try:
            _write_series_history_file(tmp_path, market_id, event_id, test_series)
            metrics = _run_backtest_once(
                history_file=tmp_path,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
            )
            rows.append(
                {
                    'run': run_idx,
                    'train_range': [series[train_start][0], series[train_end - 1][0]],
                    'test_range': [test_series[0][0], test_series[-1][0]],
                    'ok': True,
                    'metrics': metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    'run': run_idx,
                    'train_range': [series[train_start][0], series[train_end - 1][0]],
                    'test_range': [test_series[0][0], test_series[-1][0]],
                    'ok': False,
                    'error': str(exc),
                }
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        run_idx += 1
        offset += step_size

    _write_jsonl(output, rows)
    payload = {
        'message': 'Walk-forward complete',
        'output': str(Path(output).resolve()),
        'runs': len(rows),
        'ok_runs': sum(1 for r in rows if r.get('ok')),
    }
    _emit(payload, as_json=as_json)


@research.command('stress-test')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_stress_test(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    initial_capital: str,
    output: str,
    as_json: bool,
) -> None:
    """Run execution/stability stress scenarios for one strategy."""
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    scenarios = [
        {
            'name': 'baseline',
            'min_fill_rate': Decimal('0.5'),
            'max_fill_rate': Decimal('1.0'),
            'commission_rate': Decimal('0.0'),
        },
        {
            'name': 'low_fill',
            'min_fill_rate': Decimal('0.2'),
            'max_fill_rate': Decimal('0.6'),
            'commission_rate': Decimal('0.0'),
        },
        {
            'name': 'high_fee',
            'min_fill_rate': Decimal('0.5'),
            'max_fill_rate': Decimal('1.0'),
            'commission_rate': Decimal('0.01'),
        },
        {
            'name': 'low_fill_high_fee',
            'min_fill_rate': Decimal('0.2'),
            'max_fill_rate': Decimal('0.6'),
            'commission_rate': Decimal('0.02'),
        },
    ]

    rows: list[dict[str, object]] = []
    for scenario in scenarios:
        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
                min_fill_rate=scenario['min_fill_rate'],
                max_fill_rate=scenario['max_fill_rate'],
                commission_rate=scenario['commission_rate'],
            )
            rows.append(
                {
                    'scenario': scenario['name'],
                    'ok': True,
                    'config': {
                        'min_fill_rate': str(scenario['min_fill_rate']),
                        'max_fill_rate': str(scenario['max_fill_rate']),
                        'commission_rate': str(scenario['commission_rate']),
                    },
                    'metrics': metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    'scenario': scenario['name'],
                    'ok': False,
                    'error': str(exc),
                }
            )
    _write_jsonl(output, rows)
    payload = {
        'message': 'Stress test complete',
        'output': str(Path(output).resolve()),
        'scenarios': len(rows),
    }
    _emit(payload, as_json=as_json)


@research.command('compare-runs')
@click.option(
    '--input-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    '--sort-key',
    default='sharpe_ratio',
    show_default=True,
    type=click.Choice(['sharpe_ratio', 'total_pnl', 'win_rate', 'max_drawdown']),
)
@click.option('--top', default=20, show_default=True, type=int)
@click.option('--output', default=None, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_compare_runs(
    input_file: str,
    sort_key: str,
    top: int,
    output: str | None,
    as_json: bool,
) -> None:
    """Compare run outputs and rank by the selected metric."""
    rows = _load_jsonl_rows(input_file)
    ranked: list[dict[str, object]] = []
    for i, row in enumerate(rows, start=1):
        metrics = row.get('metrics', row)
        if not isinstance(metrics, dict):
            continue
        score = _to_float_metric(metrics.get(sort_key))
        if score is None:
            continue
        ranked.append(
            {
                'rank_hint': i,
                'name': row.get('name') or row.get('scenario') or row.get('run_id'),
                'ok': bool(row.get('ok', True)),
                'score': score,
                'metrics': metrics,
            }
        )

    reverse = sort_key != 'max_drawdown'
    ranked.sort(key=lambda x: float(x['score']), reverse=reverse)
    ranked = ranked[: max(top, 1)]
    if output:
        _write_jsonl(output, ranked)
    payload = {
        'message': 'Run comparison complete',
        'sort_key': sort_key,
        'count': len(ranked),
        'top': ranked,
        'output': str(Path(output).resolve()) if output else None,
    }
    _emit(payload, as_json=as_json)


@research.command('strategy-gate')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--dry-run-events', default=10, show_default=True, type=int)
@click.option('--min-trades', default=1, show_default=True, type=int)
@click.option('--min-total-pnl', default='0', show_default=True)
@click.option('--max-drawdown-pct', default='0.30', show_default=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_strategy_gate(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    dry_run_events: int,
    min_trades: int,
    min_total_pnl: str,
    max_drawdown_pct: str,
    initial_capital: str,
    as_json: bool,
) -> None:
    """Run strategy validation gate checks before promotion."""
    min_pnl = _to_decimal(min_total_pnl)
    max_dd = _to_decimal(max_drawdown_pct)
    capital = _to_decimal(initial_capital)
    if min_pnl is None or max_dd is None or capital is None:
        raise click.ClickException('Invalid gate threshold value.')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    # Validate/load strategy
    _strategy_from_ref(strategy_ref, strategy_kwargs)

    # Dry run on mock events
    from coinjure.cli.agent_commands import _build_mock_events

    ticker = PolyMarketTicker(
        symbol='GATE_TOKEN',
        name='Gate Market',
        token_id='GATE_TOKEN',
        market_id='GATE_MKT',
        event_id='GATE_EVT',
    )
    strategy = _strategy_from_ref(strategy_ref, strategy_kwargs)
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=capital,
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

    async def _run_dry() -> tuple[bool, str]:
        for event in _build_mock_events(ticker, max(1, dry_run_events)):
            if hasattr(event, 'side'):
                market_data.process_orderbook_event(event)  # type: ignore[arg-type]
            elif hasattr(event, 'price'):
                market_data.process_price_change_event(event)  # type: ignore[arg-type]
            await strategy.process_event(event, trader)
        return True, ''

    dry_ok = True
    dry_err = ''
    try:
        asyncio.run(_run_dry())
    except Exception as exc:  # noqa: BLE001
        dry_ok = False
        dry_err = str(exc)

    metrics = _run_backtest_once(
        history_file=history_file,
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        market_id=market_id,
        event_id=event_id,
        initial_capital=capital,
    )

    trades = int(metrics.get('total_trades', 0))
    pnl = _to_decimal(metrics.get('total_pnl'))
    dd = _to_decimal(metrics.get('max_drawdown'))
    checks = {
        'dry_run_ok': dry_ok,
        'min_trades_ok': trades >= min_trades,
        'min_pnl_ok': pnl is not None and pnl >= min_pnl,
        'max_drawdown_ok': dd is not None and dd <= max_dd,
    }
    passed = all(checks.values())
    payload = {
        'passed': passed,
        'checks': checks,
        'dry_run_error': dry_err or None,
        'metrics': metrics,
        'thresholds': {
            'min_trades': min_trades,
            'min_total_pnl': str(min_pnl),
            'max_drawdown_pct': str(max_dd),
        },
        'message': 'Strategy gate passed' if passed else 'Strategy gate failed',
    }
    _emit(payload, as_json=as_json)
    if not passed:
        raise click.ClickException('Strategy gate failed')


@research.group('memory')
def research_memory() -> None:
    """Persist and query experiment memory."""


@research_memory.command('add')
@click.option(
    '--input-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    '--memory-file',
    default='data/run_memory.jsonl',
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option('--tag', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_memory_add(
    input_file: str,
    memory_file: str,
    tag: str | None,
    as_json: bool,
) -> None:
    """Append run outputs into a long-lived memory file."""
    rows = _load_jsonl_rows(input_file)
    out_path = Path(memory_file).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with out_path.open('a', encoding='utf-8') as f:
        for row in rows:
            record = {
                'recorded_at': now,
                'tag': tag,
                'source_file': str(Path(input_file).resolve()),
                'payload': row,
            }
            f.write(json.dumps(record) + '\n')
    payload = {
        'message': 'Memory updated',
        'memory_file': str(out_path),
        'rows_added': len(rows),
    }
    _emit(payload, as_json=as_json)


@research_memory.command('list')
@click.option(
    '--memory-file',
    default='data/run_memory.jsonl',
    show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--tag', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_memory_list(
    memory_file: str,
    limit: int,
    tag: str | None,
    as_json: bool,
) -> None:
    """List recent memory records."""
    path = Path(memory_file).expanduser().resolve()
    if not path.exists():
        payload = {'memory_file': str(path), 'count': 0, 'records': []}
        _emit(payload, as_json=as_json)
        return
    rows = _load_jsonl_rows(str(path))
    if tag is not None:
        rows = [row for row in rows if row.get('tag') == tag]
    rows = rows[-max(limit, 1) :]
    payload = {'memory_file': str(path), 'count': len(rows), 'records': rows}
    _emit(payload, as_json=as_json)
