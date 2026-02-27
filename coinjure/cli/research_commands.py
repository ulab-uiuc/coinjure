"""Research tooling for strategy discovery and evaluation."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import click

from coinjure.cli.utils import _emit
from coinjure.core.trading_engine import TradingEngine
from coinjure.data.backtest.history_reader import iter_history_rows
from coinjure.data.backtest.historical_data_source import HistoricalDataSource
from coinjure.data.market_data_manager import MarketDataManager
from coinjure.position.position_manager import Position, PositionManager
from coinjure.risk.risk_manager import NoRiskManager, StandardRiskManager
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import CashTicker, PolyMarketTicker
from coinjure.trader.paper_trader import PaperTrader



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
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_timestamp(value: object) -> int | None:
    # Fast path for integer-like values.
    iv = _to_int(value)
    if iv is not None:
        return iv

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        # Numeric strings
        try:
            return int(float(raw))
        except Exception:  # noqa: BLE001
            pass
        # ISO 8601 strings from market history, e.g. 2026-02-20T07:45:14+00:00
        iso = raw
        if iso.endswith('Z'):
            iso = iso[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            return None
    return None


def _to_unix_ts(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        as_int = _to_int(raw)
        if as_int is not None:
            return as_int
        try:
            as_float = float(raw)
            return int(as_float)
        except ValueError:
            pass
        normalized = raw.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _load_history_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding='utf-8') as f:
        content = f.read()

    if not content.strip():
        return []

    lead = content.lstrip()[:1]
    if lead in {'[', '{'}:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            if isinstance(parsed, list):
                return [row for row in parsed if isinstance(row, dict)]
            if isinstance(parsed, dict):
                rows = parsed.get('data')
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
                return [parsed]

    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
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


def _extract_yes_points(
    row: dict[str, Any],
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[tuple[int, Decimal]]:
    yes_series = (row.get('time_series') or {}).get('Yes')
    if not isinstance(yes_series, list):
        return []

    points: list[tuple[int, Decimal]] = []
    for point in yes_series:
        if not isinstance(point, dict):
            continue
        ts = _to_unix_ts(point.get('t'))
        price = _to_decimal(point.get('p'))
        if ts is None or price is None:
            continue
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        points.append((ts, price))
    return points


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
    for row in _load_history_rows(path):
        if row.get('event_id') != event_id or row.get('market_id') != market_id:
            continue
        raw_points.extend(
            _extract_yes_points(row, start_ts=start_ts, end_ts=end_ts)
        )

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


def _write_json(output_file: str, payload: dict[str, object]) -> None:
    out = Path(output_file).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, default=str), encoding='utf-8')


def _collect_market_summaries(history_file: str) -> list[dict[str, object]]:
    path = Path(history_file).expanduser().resolve()
    if not path.exists():
        raise click.ClickException(f'History file not found: {path}')

    seen: dict[tuple[str, str], dict[str, object]] = {}
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            market_id = str(row.get('market_id', '')).strip()
            event_id = str(row.get('event_id', '')).strip()
            if not market_id or not event_id:
                continue
            key = (market_id, event_id)

            ts_points = (row.get('time_series') or {}).get('Yes')
            if not isinstance(ts_points, list):
                ts_points = []

            parsed_ts = []
            for point in ts_points:
                if not isinstance(point, dict):
                    continue
                ts = _to_timestamp(point.get('t'))
                if ts is not None:
                    parsed_ts.append(ts)

            cur = seen.setdefault(
                key,
                {
                    'market_id': market_id,
                    'event_id': event_id,
                    'question': row.get('question') or row.get('event_title') or '',
                    'volume': _to_decimal(row.get('volume')) or Decimal('0'),
                    'rows': 0,
                    'points': 0,
                    'first_ts': None,
                    'last_ts': None,
                },
            )
            cur['rows'] = int(cur['rows']) + 1
            cur['points'] = int(cur['points']) + len(parsed_ts)
            if not cur.get('question'):
                cur['question'] = row.get('question') or row.get('event_title') or ''

            vol = _to_decimal(row.get('volume'))
            if vol is not None and vol > (cur['volume'] or Decimal('0')):  # type: ignore[operator]
                cur['volume'] = vol

            if parsed_ts:
                lo = min(parsed_ts)
                hi = max(parsed_ts)
                first_ts = cur.get('first_ts')
                last_ts = cur.get('last_ts')
                cur['first_ts'] = lo if first_ts is None else min(int(first_ts), lo)
                cur['last_ts'] = hi if last_ts is None else max(int(last_ts), hi)

    rows: list[dict[str, object]] = []
    for rec in seen.values():
        first_ts = rec.get('first_ts')
        last_ts = rec.get('last_ts')
        span = (int(last_ts) - int(first_ts)) if first_ts is not None and last_ts is not None else None
        rows.append(
            {
                'market_id': rec['market_id'],
                'event_id': rec['event_id'],
                'question': rec.get('question') or '',
                'volume': str(rec.get('volume') or Decimal('0')),
                'rows': int(rec.get('rows') or 0),
                'points': int(rec.get('points') or 0),
                'first_ts': first_ts,
                'last_ts': last_ts,
                'span_seconds': span,
            }
        )
    return rows


def _sort_market_summaries(
    rows: list[dict[str, object]],
    *,
    sort_by: str,
) -> list[dict[str, object]]:
    if sort_by == 'file':
        return rows
    if sort_by == 'points':
        return sorted(rows, key=lambda r: int(r.get('points') or 0), reverse=True)
    if sort_by == 'span':
        return sorted(rows, key=lambda r: int(r.get('span_seconds') or 0), reverse=True)
    if sort_by == 'volume':
        return sorted(
            rows,
            key=lambda r: float(_to_decimal(r.get('volume')) or Decimal('0')),
            reverse=True,
        )
    raise click.ClickException(f'Unsupported sort key: {sort_by}')


def _run_strategy_dry_run(
    *,
    strategy_ref: str,
    strategy_kwargs: dict[str, Any],
    initial_capital: Decimal,
    dry_run_events: int,
) -> dict[str, object]:
    from coinjure.cli.agent_commands import _build_mock_events

    ticker = PolyMarketTicker(
        symbol='DRYRUN_YES',
        name='Dry Run Market',
        token_id='DRYRUN_YES',
        market_id='DRYRUN_MKT',
        event_id='DRYRUN_EVT',
        no_token_id='DRYRUN_NO',
    )
    strategy = _strategy_from_ref(strategy_ref, strategy_kwargs)
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
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )
    event_stream = _build_mock_events(ticker, max(1, dry_run_events))

    async def _run_stream() -> tuple[int, str]:
        processed = 0
        error_message = ''
        for event in event_stream:
            if hasattr(event, 'side'):
                market_data.process_orderbook_event(event)  # type: ignore[arg-type]
            elif hasattr(event, 'price'):
                market_data.process_price_change_event(event)  # type: ignore[arg-type]
            try:
                await strategy.process_event(event, trader)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                break
        return processed, error_message

    processed, error_message = asyncio.run(_run_stream())
    return {
        'ok': error_message == '',
        'events_requested': max(1, dry_run_events),
        'events_processed': processed,
        'orders_created': len(trader.orders),
        'decision_stats': strategy.get_decision_stats(),
        'error': error_message or None,
    }


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


def _build_market_summaries(
    history_file: str, *, min_points: int
) -> list[dict[str, object]]:
    path = Path(history_file).expanduser().resolve()
    grouped: dict[tuple[str, str], dict[int, Decimal]] = {}
    for row in _load_history_rows(path):
        market_id = str(row.get('market_id', '')).strip()
        event_id = str(row.get('event_id', '')).strip()
        if not market_id or not event_id:
            continue
        key = (market_id, event_id)
        bucket = grouped.setdefault(key, {})
        for ts, price in _extract_yes_points(row):
            bucket[ts] = price

    summaries: list[dict[str, object]] = []
    for (market_id, event_id), points in grouped.items():
        if len(points) < min_points:
            continue
        series = sorted(points.items(), key=lambda x: x[0])
        prices = [p for _, p in series]
        start_price = prices[0]
        end_price = prices[-1]
        high = max(prices)
        low = min(prices)
        summaries.append(
            {
                'market_id': market_id,
                'event_id': event_id,
                'points': len(series),
                'first_ts': series[0][0],
                'last_ts': series[-1][0],
                'start_price': str(start_price),
                'end_price': str(end_price),
                'abs_move': str(abs(end_price - start_price)),
                'price_range': str(high - low),
            }
        )
    return summaries


def _to_float_metric(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _fit_train_test_windows(
    *,
    n_points: int,
    train_size: int,
    test_size: int,
) -> tuple[int, int, bool]:
    """Fit train/test windows to available points.

    Returns:
        (train_size, test_size, resized)
    """
    if n_points < 2:
        raise click.ClickException('Not enough points for walk-forward.')
    if train_size + test_size <= n_points:
        return train_size, test_size, False

    resized_train = max(1, int(n_points * 0.7))
    resized_test = max(1, n_points - resized_train)
    if resized_train + resized_test > n_points:
        resized_test = max(1, n_points - resized_train)
    if resized_train + resized_test > n_points:
        resized_train = max(1, n_points - resized_test)
    if resized_train + resized_test > n_points or resized_train <= 0 or resized_test <= 0:
        raise click.ClickException('Not enough points for auto-sized walk-forward.')
    return resized_train, resized_test, True


def _alpha_score_from_metrics(metrics: dict[str, Any]) -> float | None:
    """Composite score balancing return, drawdown, and turnover."""
    pnl = _to_float_metric(metrics.get('total_pnl'))
    max_drawdown = _to_float_metric(metrics.get('max_drawdown'))
    total_trades = _to_float_metric(metrics.get('total_trades'))
    if pnl is None or max_drawdown is None or total_trades is None:
        return None

    turnover_penalty = 0.02 * total_trades
    drawdown_penalty = 250.0 * max(0.0, max_drawdown)
    return pnl - drawdown_penalty - turnover_penalty


@click.group()
def research() -> None:
    """Research and strategy-discovery tooling."""


@research.command('markets')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    '--sort-by',
    default='points',
    show_default=True,
    type=click.Choice(['file', 'points', 'volume', 'span']),
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option(
    '--min-points',
    default=0,
    show_default=True,
    type=int,
    help='Only keep markets with at least this many data points.',
)
@click.option(
    '--min-volume',
    default='0',
    show_default=True,
    help='Only keep markets with volume >= this threshold.',
)
@click.option(
    '--min-span-seconds',
    default=0,
    show_default=True,
    type=int,
    help='Only keep markets with span_seconds >= this threshold.',
)
@click.option('--output', default=None, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_markets(
    history_file: str,
    sort_by: str,
    limit: int,
    min_points: int,
    min_volume: str,
    min_span_seconds: int,
    output: str | None,
    as_json: bool,
) -> None:
    """Summarize and rank available markets in a history dataset."""
    if min_points < 0 or min_span_seconds < 0:
        raise click.ClickException('--min-points and --min-span-seconds must be >= 0')
    min_vol = _to_decimal(min_volume)
    if min_vol is None:
        raise click.ClickException(f'Invalid --min-volume: {min_volume}')

    rows = _collect_market_summaries(history_file)
    rows = [
        row
        for row in rows
        if int(row.get('points') or 0) >= min_points
        and int(row.get('span_seconds') or 0) >= min_span_seconds
        and (_to_decimal(row.get('volume')) or Decimal('0')) >= min_vol
    ]
    ranked = _sort_market_summaries(rows, sort_by=sort_by)[: max(1, limit)]
    if output:
        _write_jsonl(output, ranked)
    payload = {
        'message': 'Market scan complete',
        'history_file': str(Path(history_file).resolve()),
        'sort_by': sort_by,
        'filters': {
            'min_points': min_points,
            'min_volume': str(min_vol),
            'min_span_seconds': min_span_seconds,
        },
        'count': len(ranked),
        'markets': ranked,
        'output': str(Path(output).resolve()) if output else None,
    }
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


@research.command('scan-markets')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option(
    '--params-jsonl', default=None, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--max-markets', default=20, show_default=True, type=int)
@click.option('--min-points', default=20, show_default=True, type=int)
@click.option(
    '--sort-key',
    default='price_range',
    show_default=True,
    type=click.Choice(['price_range', 'abs_move', 'points']),
)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_scan_markets(
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    params_jsonl: str | None,
    initial_capital: str,
    max_markets: int,
    min_points: int,
    sort_key: str,
    output: str,
    as_json: bool,
) -> None:
    """Scan many market/event pairs and keep the best run per market."""
    if max_markets <= 0:
        raise click.ClickException('--max-markets must be > 0')
    if min_points <= 1:
        raise click.ClickException('--min-points must be > 1')

    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    base_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    if params_jsonl:
        param_rows = _load_jsonl_rows(params_jsonl)
        if not param_rows:
            raise click.ClickException('No valid rows found in --params-jsonl')
    else:
        param_rows = [{'id': 'default', 'strategy_kwargs': base_kwargs}]

    summaries = _build_market_summaries(history_file, min_points=min_points)
    if not summaries:
        raise click.ClickException('No markets matched --min-points constraint.')

    reverse = True
    if sort_key == 'points':
        summaries.sort(key=lambda x: int(x['points']), reverse=reverse)
    else:
        summaries.sort(
            key=lambda x: float(x[sort_key]),  # type: ignore[arg-type]
            reverse=reverse,
        )
    selected = summaries[:max_markets]

    rows: list[dict[str, object]] = []
    for market in selected:
        market_id = str(market['market_id'])
        event_id = str(market['event_id'])
        best_result: dict[str, object] | None = None
        best_key: tuple[float, float] | None = None

        for idx, row in enumerate(param_rows, start=1):
            strategy_kwargs = row.get('strategy_kwargs')
            if not isinstance(strategy_kwargs, dict):
                strategy_kwargs = {
                    k: v for k, v in row.items() if k not in {'id', 'name', 'run_id'}
                }
            merged_kwargs = {**base_kwargs, **strategy_kwargs}
            run_id = str(row.get('id') or row.get('run_id') or f'run-{idx}')
            run_name = str(row.get('name') or run_id)
            try:
                metrics = _run_backtest_once(
                    history_file=history_file,
                    strategy_ref=strategy_ref,
                    strategy_kwargs=merged_kwargs,
                    market_id=market_id,
                    event_id=event_id,
                    initial_capital=capital,
                )
            except Exception as exc:  # noqa: BLE001
                metrics = None
                candidate_result = {
                    'run_id': run_id,
                    'name': run_name,
                    'strategy_kwargs': merged_kwargs,
                    'ok': False,
                    'error': str(exc),
                }
                if best_result is None:
                    best_result = candidate_result
                continue

            pnl = _to_float_metric(metrics.get('total_pnl')) or float('-inf')
            sharpe = _to_float_metric(metrics.get('sharpe_ratio')) or float('-inf')
            rank_key = (pnl, sharpe)
            candidate_result = {
                'run_id': run_id,
                'name': run_name,
                'strategy_kwargs': merged_kwargs,
                'ok': True,
                'metrics': metrics,
            }
            if best_key is None or rank_key > best_key:
                best_key = rank_key
                best_result = candidate_result

        rows.append(
            {
                'market_id': market_id,
                'event_id': event_id,
                'points': market['points'],
                'first_ts': market['first_ts'],
                'last_ts': market['last_ts'],
                'abs_move': market['abs_move'],
                'price_range': market['price_range'],
                'best_run': best_result,
            }
        )

    _write_jsonl(output, rows)
    payload = {
        'message': 'Market scan complete',
        'output': str(Path(output).resolve()),
        'markets_scanned': len(rows),
        'markets_with_successful_run': sum(
            1 for row in rows if bool((row.get('best_run') or {}).get('ok'))
        ),
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
    n_points = len(series)
    train_size, test_size, auto_resized = _fit_train_test_windows(
        n_points=n_points,
        train_size=train_size,
        test_size=test_size,
    )
    step_size = max(1, step_size)

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
        'n_points': n_points,
        'train_size': train_size,
        'test_size': test_size,
        'step_size': step_size,
        'auto_resized': auto_resized,
        'runs': len(rows),
        'ok_runs': sum(1 for r in rows if r.get('ok')),
    }
    _emit(payload, as_json=as_json)


@research.command('walk-forward-auto')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--min-train-size', default=300, show_default=True, type=int)
@click.option('--min-test-size', default=120, show_default=True, type=int)
@click.option('--target-runs', default=5, show_default=True, type=int)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_walk_forward_auto(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    min_train_size: int,
    min_test_size: int,
    target_runs: int,
    initial_capital: str,
    output: str,
    as_json: bool,
) -> None:
    """Run walk-forward with automatically sized windows for the available history."""
    if min(min_train_size, min_test_size, target_runs) <= 0:
        raise click.ClickException('min-train-size/min-test-size/target-runs must all be > 0')
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    series = _load_yes_series(history_file, market_id, event_id)
    n_points = len(series)
    train_size, test_size, auto_resized = _fit_train_test_windows(
        n_points=n_points,
        train_size=min_train_size,
        test_size=min_test_size,
    )

    slack = n_points - train_size - test_size
    if target_runs <= 1:
        step_size = max(1, slack + 1)
    else:
        step_size = max(1, slack // (target_runs - 1)) if slack > 0 else 1

    rows: list[dict[str, object]] = []
    offset = 0
    run_idx = 1
    while offset + train_size + test_size <= n_points and run_idx <= target_runs:
        train_start = offset
        train_end = offset + train_size
        test_end = train_end + test_size
        test_series = series[train_end:test_end]
        if not test_series:
            break

        tmp_file = tempfile.NamedTemporaryFile(
            prefix='coinjure_walk_forward_auto_',
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
        'message': 'Auto walk-forward complete',
        'output': str(Path(output).resolve()),
        'n_points': n_points,
        'train_size': train_size,
        'test_size': test_size,
        'step_size': step_size,
        'auto_resized': auto_resized,
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
    type=click.Choice(
        ['sharpe_ratio', 'total_pnl', 'win_rate', 'max_drawdown', 'alpha_score']
    ),
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
        if sort_key == 'alpha_score':
            score = _alpha_score_from_metrics(metrics)
        else:
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


@research.command('alpha-pipeline')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--market-id', default=None)
@click.option('--event-id', default=None)
@click.option(
    '--market-sort-by',
    default='points',
    show_default=True,
    type=click.Choice(['file', 'points', 'volume', 'span']),
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
@click.option('--min-trades', default=1, show_default=True, type=int)
@click.option('--min-total-pnl', default='0', show_default=True)
@click.option('--max-drawdown-pct', default='0.30', show_default=True)
@click.option('--batch-limit', default=20, show_default=True, type=int)
@click.option('--run-batch-markets/--no-run-batch-markets', default=True)
@click.option(
    '--artifacts-dir',
    default='data/research/alpha_pipeline',
    show_default=True,
    type=click.Path(file_okay=False),
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_alpha_pipeline(
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    market_id: str | None,
    event_id: str | None,
    market_sort_by: str,
    market_rank: int,
    dry_run_events: int,
    initial_capital: str,
    min_trades: int,
    min_total_pnl: str,
    max_drawdown_pct: str,
    batch_limit: int,
    run_batch_markets: bool,
    artifacts_dir: str,
    as_json: bool,
) -> None:
    """Run validate + backtest + stress + gate (+ optional batch) in one command."""
    if dry_run_events <= 0:
        raise click.ClickException('--dry-run-events must be > 0')
    if market_rank <= 0:
        raise click.ClickException('--market-rank must be > 0')
    if batch_limit <= 0:
        raise click.ClickException('--batch-limit must be > 0')

    capital = _to_decimal(initial_capital)
    min_pnl = _to_decimal(min_total_pnl)
    max_dd = _to_decimal(max_drawdown_pct)
    if capital is None or min_pnl is None or max_dd is None:
        raise click.ClickException('Invalid capital or gate threshold value.')
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
            'rank': market_rank,
            'sort_by': market_sort_by,
            'question': selected.get('question') or '',
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
        payload = {
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
    )
    single_path = out_dir / 'backtest_single.json'
    _write_json(str(single_path), metrics)

    stress_rows: list[dict[str, object]] = []
    stress_scenarios = [
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
    for scenario in stress_scenarios:
        try:
            scenario_metrics = _run_backtest_once(
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
            stress_rows.append(
                {
                    'scenario': scenario['name'],
                    'ok': True,
                    'metrics': scenario_metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            stress_rows.append({'scenario': scenario['name'], 'ok': False, 'error': str(exc)})
    stress_path = out_dir / 'stress.jsonl'
    _write_jsonl(str(stress_path), stress_rows)

    trades = int(metrics.get('total_trades', 0))
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
    if run_batch_markets:
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
        'message': 'Alpha pipeline complete' if gate_passed else 'Alpha pipeline gate failed',
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
    _emit(payload, as_json=as_json)
    if not gate_passed:
        raise click.ClickException('Alpha pipeline gate failed.')


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


@research.command('batch-markets')
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
@click.option('--limit', default=50, show_default=True, type=int, help='Max markets to test.')
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_batch_markets(
    history_file: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    initial_capital: str,
    limit: int,
    output: str,
    as_json: bool,
) -> None:
    """Run one strategy across N markets and return per-market results + aggregate stats."""
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )

    # Collect distinct (market_id, event_id) pairs from the history file.
    seen: dict[tuple[str, str], None] = {}
    path = Path(history_file).expanduser().resolve()
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = str(row.get('market_id', ''))
            eid = str(row.get('event_id', ''))
            if mid and eid:
                seen[(mid, eid)] = None
            if len(seen) >= limit:
                break
    pairs = list(seen.keys())[:limit]

    if not pairs:
        raise click.ClickException('No valid (market_id, event_id) pairs found in history file.')

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
    win_rates = [_to_float_metric((r['metrics'] or {}).get('win_rate')) for r in ok_results if isinstance(r.get('metrics'), dict)]  # type: ignore[index]
    sharpes = [_to_float_metric((r['metrics'] or {}).get('sharpe_ratio')) for r in ok_results if isinstance(r.get('metrics'), dict)]  # type: ignore[index]
    pnls = [_to_float_metric((r['metrics'] or {}).get('total_pnl')) for r in ok_results if isinstance(r.get('metrics'), dict)]  # type: ignore[index]
    win_rates_f = [v for v in win_rates if v is not None]
    sharpes_f = [v for v in sharpes if v is not None]
    pnls_f = [v for v in pnls if v is not None]
    pct_profitable = (sum(1 for v in pnls_f if v > 0) / len(pnls_f) * 100) if pnls_f else 0.0

    aggregate: dict[str, object] = {
        'mean_win_rate': str(round(mean(win_rates_f), 4)) if win_rates_f else None,
        'mean_sharpe': str(round(mean(sharpes_f), 4)) if sharpes_f else None,
        'stddev_sharpe': str(round(pstdev(sharpes_f), 4)) if len(sharpes_f) > 1 else None,
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


@research.command('grid')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--strategy-ref', required=True)
@click.option(
    '--param-grid-json',
    required=True,
    help='JSON object mapping param names to lists of values, e.g. {"threshold":[0.01,0.05]}',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--max-runs', default=100, show_default=True, type=int)
@click.option(
    '--sort-key',
    default='sharpe_ratio',
    show_default=True,
    type=click.Choice(['sharpe_ratio', 'total_pnl', 'win_rate', 'max_drawdown']),
)
@click.option('--output', required=True, type=click.Path(dir_okay=False))
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_grid(
    history_file: str,
    market_id: str,
    event_id: str,
    strategy_ref: str,
    param_grid_json: str,
    initial_capital: str,
    max_runs: int,
    sort_key: str,
    output: str,
    as_json: bool,
) -> None:
    """Grid search over strategy hyperparameters on one market."""
    capital = _to_decimal(initial_capital)
    if capital is None:
        raise click.ClickException(f'Invalid --initial-capital: {initial_capital}')
    param_grid = _parse_json_object(param_grid_json, option_name='--param-grid-json')
    for key, vals in param_grid.items():
        if not isinstance(vals, list):
            raise click.ClickException(
                f'--param-grid-json: value for "{key}" must be a list.'
            )

    if not param_grid:
        combos: list[dict[str, Any]] = [{}]
    else:
        keys = list(param_grid.keys())
        value_lists = [param_grid[k] for k in keys]
        combos = [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]

    if max_runs > 0:
        combos = combos[:max_runs]

    results: list[dict[str, object]] = []
    for idx, kwargs in enumerate(combos, start=1):
        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
            )
            results.append(
                {
                    'run': idx,
                    'strategy_kwargs': kwargs,
                    'ok': True,
                    'metrics': metrics,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    'run': idx,
                    'strategy_kwargs': kwargs,
                    'ok': False,
                    'error': str(exc),
                }
            )

    _write_jsonl(output, results)

    ok_results = [r for r in results if r.get('ok')]
    best: dict[str, object] | None = None
    if ok_results:
        reverse = sort_key != 'max_drawdown'
        def _score(r: dict[str, object]) -> float:
            v = _to_float_metric((r.get('metrics') or {}).get(sort_key))  # type: ignore[arg-type]
            return v if v is not None else (float('-inf') if reverse else float('inf'))
        best_run = max(ok_results, key=_score) if reverse else min(ok_results, key=_score)
        best = {**best_run.get('strategy_kwargs', {}), **best_run.get('metrics', {})}  # type: ignore[arg-type]

    payload = {
        'ok': True,
        'runs': len(results),
        'ok_runs': len(ok_results),
        'best': best,
        'sort_key': sort_key,
        'output': str(Path(output).resolve()),
    }
    _emit(payload, as_json=as_json)
