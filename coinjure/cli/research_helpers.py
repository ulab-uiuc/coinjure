"""Shared research helper functions used by strategy, backtest, and paper commands."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import click

from coinjure.cli.utils import _emit
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

logger = logging.getLogger(__name__)


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
    iv = _to_int(value)
    if iv is not None:
        return iv
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(float(raw))
        except Exception:  # noqa: BLE001
            pass
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


def _strategy_from_ref(strategy_ref: str, strategy_kwargs: dict[str, Any]) -> Strategy:
    from coinjure.cli.agent_commands import _load_strategy

    return _load_strategy(strategy_ref, strategy_kwargs)


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


def _to_float_metric(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None


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
