"""Research tooling for strategy discovery and evaluation."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import random
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import click

logger = logging.getLogger(__name__)

from coinjure.cli.utils import _emit
from coinjure.engine.live_trader import run_live_paper_trading
from coinjure.engine.trading_engine import TradingEngine
from coinjure.market.backtest.historical_data_source import HistoricalDataSource
from coinjure.market.market_data_manager import MarketDataManager
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import CashTicker, PolyMarketTicker, Ticker
from coinjure.trading.paper_trader import PaperTrader
from coinjure.trading.position_manager import Position, PositionManager
from coinjure.trading.risk_manager import (
    NoRiskManager,
    RiskManager,
    StandardRiskManager,
)


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


def _load_history_rows(path: Path) -> list[dict[str, Any]]:  # noqa: C901
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
                data_rows = parsed.get('data')
                if isinstance(data_rows, list):
                    return [row for row in data_rows if isinstance(row, dict)]
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
        raw_points.extend(_extract_yes_points(row, start_ts=start_ts, end_ts=end_ts))

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


def _collect_market_summaries(history_file: str) -> list[dict[str, object]]:  # noqa: C901
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

            parsed_ts: list[int] = []
            parsed_prices: list[float] = []
            for point in ts_points:
                if not isinstance(point, dict):
                    continue
                ts = _to_timestamp(point.get('t'))
                price = _to_decimal(point.get('p'))
                if ts is not None:
                    parsed_ts.append(ts)
                if price is not None:
                    parsed_prices.append(float(price))

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
                    '_prices': [],
                },
            )
            cur['rows'] = int(cur['rows']) + 1  # type: ignore[call-overload]
            cur['points'] = int(cur['points']) + len(parsed_ts)  # type: ignore[call-overload]
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
                cur['first_ts'] = lo if first_ts is None else min(int(first_ts), lo)  # type: ignore[call-overload]
                cur['last_ts'] = hi if last_ts is None else max(int(last_ts), hi)  # type: ignore[call-overload]

            if parsed_prices:
                cur['_prices'] = list(cur['_prices']) + parsed_prices  # type: ignore[call-overload]

    rows: list[dict[str, object]] = []
    for rec in seen.values():
        first_ts = rec.get('first_ts')
        last_ts = rec.get('last_ts')
        span = (
            (int(last_ts) - int(first_ts))  # type: ignore[call-overload]
            if first_ts is not None and last_ts is not None
            else None
        )
        prices: list[float] = list(rec.get('_prices') or [])  # type: ignore[call-overload]
        price_std = pstdev(prices) if len(prices) >= 2 else 0.0
        price_start = prices[0] if prices else None
        price_end = prices[-1] if prices else None
        abs_trend = (
            abs(price_end - price_start)
            if price_start is not None and price_end is not None
            else 0.0
        )
        rows.append(
            {
                'market_id': rec['market_id'],
                'event_id': rec['event_id'],
                'question': rec.get('question') or '',
                'volume': str(rec.get('volume') or Decimal('0')),
                'rows': int(rec.get('rows') or 0),  # type: ignore[call-overload]
                'points': int(rec.get('points') or 0),  # type: ignore[call-overload]
                'first_ts': first_ts,
                'last_ts': last_ts,
                'span_seconds': span,
                'price_std': price_std,
                'price_start': price_start,
                'price_end': price_end,
                'abs_trend': abs_trend,
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
        return sorted(rows, key=lambda r: int(r.get('points') or 0), reverse=True)  # type: ignore[call-overload]
    if sort_by == 'span':
        return sorted(rows, key=lambda r: int(r.get('span_seconds') or 0), reverse=True)  # type: ignore[call-overload]
    if sort_by == 'volume':
        return sorted(
            rows,
            key=lambda r: float(_to_decimal(r.get('volume')) or Decimal('0')),
            reverse=True,
        )
    if sort_by == 'volatility':
        return sorted(
            rows,
            key=lambda r: float(r.get('price_std') or 0.0),  # type: ignore[arg-type]
            reverse=True,
        )
    if sort_by == 'trend':
        return sorted(
            rows,
            key=lambda r: float(r.get('abs_trend') or 0.0),  # type: ignore[arg-type]
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
    spread: Decimal = Decimal('0.01'),
    risk_profile: str = 'none',
    include_all_markets_context: bool = False,
    allow_cross_market_trading: bool = False,
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
        data_source = HistoricalDataSource(
            history_file,
            ticker,
            include_all_markets=include_all_markets_context,
        )
        market_data = MarketDataManager(
            spread=spread,
            max_history_per_ticker=None,
            max_timeline_events=None,
        )
        position_manager = PositionManager()
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=initial_capital,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )
        risk_manager: RiskManager
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
        if not allow_cross_market_trading:
            tradable_tickers: list[Ticker | str] = [ticker]
            no_ticker = ticker.get_no_ticker()
            if no_ticker is not None:
                tradable_tickers.append(no_ticker)
            trader.set_allowed_tickers(tradable_tickers)
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
        return float(value)  # type: ignore[arg-type]
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
    if (
        resized_train + resized_test > n_points
        or resized_train <= 0
        or resized_test <= 0
    ):
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


def _build_gate_checks(
    *,
    metrics: dict[str, Any],
    min_trades: int,
    min_total_pnl: Decimal,
    max_drawdown_pct: Decimal,
) -> tuple[bool, dict[str, bool]]:
    trades = int(metrics.get('total_trades', 0))
    pnl = _to_decimal(metrics.get('total_pnl'))
    dd = _to_decimal(metrics.get('max_drawdown'))
    checks = {
        'min_trades_ok': trades >= min_trades,
        'min_pnl_ok': pnl is not None and pnl >= min_total_pnl,
        'max_drawdown_ok': dd is not None and dd <= max_drawdown_pct,
    }
    return all(checks.values()), checks


_STRESS_SCENARIOS: list[dict[str, object]] = [
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


def _build_param_combos(param_grid_json: str | None) -> list[dict[str, Any]]:
    if not param_grid_json:
        return [{}]
    param_grid = _parse_json_object(param_grid_json, option_name='--param-grid-json')
    for key, vals in param_grid.items():
        if not isinstance(vals, list):
            raise click.ClickException(
                f'--param-grid-json: value for "{key}" must be a list.'
            )
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    return [
        dict(zip(keys, combo, strict=False))
        for combo in itertools.product(*value_lists)
    ]


@click.group()
def research() -> None:
    """Research and strategy-discovery tooling."""


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

    trades = int(metrics.get('total_trades', 0))  # type: ignore[call-overload]
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
    help='Synthetic bid/ask half-spread for the backtest MarketDataManager (e.g. 0.003).',
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
def research_alpha_pipeline(  # noqa: C901
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

        entry = LedgerEntry(
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
        ExperimentLedger().append(entry)
    except Exception:  # noqa: BLE001
        logger.warning('Failed to auto-record to experiment ledger', exc_info=True)

    _emit(payload, as_json=as_json)
    if not gate_passed:
        raise click.ClickException('Alpha pipeline gate failed.')


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
@click.option(
    '--limit', default=50, show_default=True, type=int, help='Max markets to test.'
)
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


# ===================================================================
# memory — persistent experiment ledger
# ===================================================================


@research.group('memory')
def research_memory() -> None:
    """Persistent experiment memory (ledger)."""


@research_memory.command('add')
@click.option('--run-id', required=True, help='Unique experiment identifier.')
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--market-id', default='')
@click.option('--event-id', default='')
@click.option('--history-file', default='')
@click.option('--gate-passed', is_flag=True, default=False)
@click.option('--metrics-json', default='{}', help='JSON object of metric values.')
@click.option('--tag', multiple=True, help='Tags (can repeat).')
@click.option('--notes', default='')
@click.option('--artifacts-dir', default='')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_add(
    run_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    market_id: str,
    event_id: str,
    history_file: str,
    gate_passed: bool,
    metrics_json: str,
    tag: tuple[str, ...],
    notes: str,
    artifacts_dir: str,
    as_json: bool,
) -> None:
    """Append an experiment result to the ledger."""
    from coinjure.research.ledger import ExperimentLedger, LedgerEntry

    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )
    metrics = _parse_json_object(metrics_json, option_name='--metrics-json')
    entry = LedgerEntry(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        market_id=market_id,
        event_id=event_id,
        history_file=history_file,
        gate_passed=gate_passed,
        metrics=metrics,
        tags=list(tag),
        notes=notes,
        artifacts_dir=artifacts_dir,
    )
    ExperimentLedger().append(entry)
    _emit({'ok': True, 'run_id': run_id, 'entry': entry.to_dict()}, as_json=as_json)


@research_memory.command('list')
@click.option('--tag', default=None, help='Filter by tag.')
@click.option(
    '--strategy-ref', default=None, help='Filter by strategy ref (substring).'
)
@click.option('--market-id', default=None, help='Filter by exact market ID.')
@click.option('--gate-passed', is_flag=True, default=False, help='Only gate-passed.')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_list(
    tag: str | None,
    strategy_ref: str | None,
    market_id: str | None,
    gate_passed: bool,
    as_json: bool,
) -> None:
    """List experiments from the ledger with optional filters."""
    from coinjure.research.ledger import ExperimentLedger

    entries = ExperimentLedger().query(
        tag=tag,
        strategy_ref=strategy_ref,
        market_id=market_id,
        gate_passed=gate_passed if gate_passed else None,
    )
    _emit(
        {'ok': True, 'count': len(entries), 'entries': [e.to_dict() for e in entries]},
        as_json=as_json,
    )


@research_memory.command('best')
@click.option(
    '--metric', default='total_pnl', show_default=True, help='Metric key to rank by.'
)
@click.option('--top', default=5, show_default=True, type=int)
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_best(metric: str, top: int, as_json: bool) -> None:
    """Return top-N experiments by a metric."""
    from coinjure.research.ledger import ExperimentLedger

    entries = ExperimentLedger().best(metric_key=metric, top_n=top)
    _emit(
        {
            'ok': True,
            'metric': metric,
            'count': len(entries),
            'entries': [e.to_dict() for e in entries],
        },
        as_json=as_json,
    )


@research_memory.command('summary')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_summary(as_json: bool) -> None:
    """Aggregate statistics across all experiments."""
    from coinjure.research.ledger import ExperimentLedger

    summary = ExperimentLedger().summary()
    _emit({'ok': True, **summary}, as_json=as_json)


# ===================================================================
# harvest / feedback-report — paper vs backtest comparison
# ===================================================================


@research.command('harvest')
@click.option('--strategy-id', required=True, help='Portfolio strategy ID.')
@click.option(
    '--socket-path',
    default=None,
    help='Control socket path (default: ~/.coinjure/<strategy-id>.sock).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_harvest(strategy_id: str, socket_path: str | None, as_json: bool) -> None:
    """Harvest current paper/live performance and save to feedback ledger."""
    from coinjure.cli.control import run_command
    from coinjure.research.ledger import FeedbackEntry, FeedbackLedger

    sock = socket_path or str(Path.home() / '.coinjure' / f'{strategy_id}.sock')
    if not Path(sock).exists():
        raise click.ClickException(f'Socket not found: {sock}')

    resp = run_command('status', socket_path=Path(sock))
    if not resp.get('ok'):
        raise click.ClickException(
            f'Status query failed: {resp.get("error", "unknown")}'
        )

    portfolio = resp.get('portfolio', {})
    pnl = None
    for pos in portfolio.get('non_cash', []):
        if 'unrealized_pnl' in pos:
            try:
                pnl = (pnl or 0.0) + float(pos['unrealized_pnl'])
            except (TypeError, ValueError):
                pass
    realized = None
    try:
        realized = float(portfolio.get('realized_pnl', 0))
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
    _emit(
        {'ok': True, 'strategy_id': strategy_id, 'entry': entry.to_dict()},
        as_json=as_json,
    )


@research.command('feedback-report')
@click.option('--strategy-id', required=True, help='Portfolio strategy ID.')
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_feedback_report(strategy_id: str, as_json: bool) -> None:
    """Compare latest paper performance against backtest predictions."""
    from coinjure.research.ledger import ExperimentLedger, FeedbackLedger

    feedback = FeedbackLedger().latest(strategy_id)
    if feedback is None:
        raise click.ClickException(
            f'No feedback entries for {strategy_id}. Run `research harvest` first.'
        )

    # Find matching experiment by strategy_id as run_id
    experiments = ExperimentLedger().query(strategy_ref=strategy_id)
    if not experiments:
        # Fallback: try matching by run_id
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
    paper_pnl = _safe_float(feedback.metrics.get('realized_pnl'))

    report: dict[str, Any] = {
        'ok': True,
        'strategy_id': strategy_id,
        'backtest': {
            'total_pnl': bt_pnl,
            'sharpe_ratio': _safe_float(backtest_metrics.get('sharpe_ratio')),
            'max_drawdown': _safe_float(backtest_metrics.get('max_drawdown')),
        },
        'paper': {
            'realized_pnl': paper_pnl,
            'unrealized_pnl': _safe_float(feedback.metrics.get('unrealized_pnl')),
            'runtime_seconds': feedback.runtime_seconds,
            'event_count': feedback.metrics.get('event_count'),
        },
        'comparison': {},
    }
    if bt_pnl is not None and paper_pnl is not None:
        report['comparison']['pnl_gap'] = round(paper_pnl - bt_pnl, 6)
    report['comparison']['decision_stats'] = feedback.decision_stats
    _emit(report, as_json=as_json)


# ===================================================================
# market-snapshot — situational awareness in one command
# ===================================================================


@research.command('market-snapshot')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--query', 'search_query', default=None, help='Optional search filter.')
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--json', 'as_json', is_flag=True, default=False)
def research_market_snapshot(
    exchange: str,
    search_query: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """One-shot market intelligence: movers, arb edges, portfolio & memory overlap."""
    from coinjure.portfolio.registry import StrategyRegistry
    from coinjure.research.ledger import ExperimentLedger

    snapshot: dict[str, Any] = {
        'ok': True,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'exchange': exchange,
    }

    # 1. Fetch markets (best-effort)
    markets: list[dict[str, Any]] = []
    try:
        if exchange == 'polymarket':
            from coinjure.market.live.live_data_source import LivePolyMarketDataSource

            ds = LivePolyMarketDataSource(polling_interval=0)
            raw_markets = (
                asyncio.get_event_loop().run_until_complete(ds._fetch_markets())
                if hasattr(ds, '_fetch_markets')
                else []
            )
            for m in raw_markets[:limit]:
                markets.append(
                    {
                        'market_id': getattr(m, 'market_id', str(m)),
                        'title': getattr(m, 'question', getattr(m, 'title', '')),
                    }
                )
    except Exception:  # noqa: BLE001
        snapshot['markets_error'] = 'Failed to fetch live markets'

    snapshot['markets_count'] = len(markets)

    # 2. Portfolio overlap
    try:
        registry = StrategyRegistry()
        active = [
            e.to_dict()
            for e in registry.list()
            if e.lifecycle in ('paper_trading', 'live_trading')
        ]
        snapshot['active_portfolio'] = active
        snapshot['active_count'] = len(active)
    except Exception:  # noqa: BLE001
        snapshot['active_portfolio'] = []
        snapshot['active_count'] = 0

    # 3. Memory overlap — what markets have been tested before
    try:
        ledger = ExperimentLedger()
        summary = ledger.summary()
        recent_best = ledger.best(metric_key='total_pnl', top_n=5)
        snapshot['memory_summary'] = summary
        snapshot['memory_top5'] = [
            {
                'run_id': e.run_id,
                'strategy_ref': e.strategy_ref,
                'market_id': e.market_id,
                'gate_passed': e.gate_passed,
                'pnl': e.metrics.get('total_pnl'),
            }
            for e in recent_best
        ]
    except Exception:  # noqa: BLE001
        snapshot['memory_summary'] = {'total_experiments': 0}
        snapshot['memory_top5'] = []

    _emit(snapshot, as_json=as_json)
