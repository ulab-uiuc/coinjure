"""Backtest engine — runs strategies against historical data for market relations.

Data sources:
  - Price history from CLOB API (default, lower granularity)
  - Parquet orderbook files (optional, higher granularity)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from coinjure.data.manager import DataManager
from coinjure.data.source import DataSource
from coinjure.engine.engine import TradingEngine
from coinjure.engine.trader.paper import PaperTrader
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.market.relations import MarketRelation
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import CashTicker, KalshiTicker, PolyMarketTicker, Ticker
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import NoRiskManager

logger = logging.getLogger(__name__)

STRUCTURAL_TYPES = frozenset(
    {'same_event', 'complementary', 'implication', 'exclusivity'}
)
STATISTICAL_TYPES = frozenset({'correlated', 'structural', 'conditional', 'temporal'})

# Synthetic half-spread for generating bid/ask from mid price
_HALF_SPREAD = Decimal('0.005')
_BOOK_SIZE = Decimal('1000')


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """Outcome of a single relation backtest."""

    relation_id: str
    spread_type: str
    strategy_name: str
    total_pnl: Decimal = Decimal('0')
    trade_count: int = 0
    passed: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# PriceHistoryDataSource — converts price points to engine events
# ---------------------------------------------------------------------------


class PriceHistoryDataSource(DataSource):
    """DataSource that replays price history as PriceChange + OrderBook events.

    Accepts two price series (one per leg of a relation) and interleaves them
    by timestamp. Generates synthetic bid/ask OrderBookEvents so PaperTrader
    has liquidity to fill against.

    If NO-side tickers are provided, also generates complementary (1 - price)
    events for each YES price point, enabling strategies that trade NO sides.
    """

    def __init__(
        self,
        ticker_a: Ticker,
        prices_a: list[dict[str, Any]],
        ticker_b: Ticker,
        prices_b: list[dict[str, Any]],
        *,
        no_ticker_a: Ticker | None = None,
        no_ticker_b: Ticker | None = None,
    ) -> None:
        self._events: list[Event] = []
        self._idx = 0
        self._build_events(
            ticker_a,
            prices_a,
            ticker_b,
            prices_b,
            no_ticker_a=no_ticker_a,
            no_ticker_b=no_ticker_b,
        )

    def _build_events(
        self,
        ticker_a: Ticker,
        prices_a: list[dict[str, Any]],
        ticker_b: Ticker,
        prices_b: list[dict[str, Any]],
        *,
        no_ticker_a: Ticker | None = None,
        no_ticker_b: Ticker | None = None,
    ) -> None:
        """Convert raw {t, p} points into engine events, sorted by timestamp."""
        raw: list[tuple[int, Ticker, Decimal]] = []
        for pt in prices_a:
            try:
                price = Decimal(str(pt['p']))
                raw.append((int(pt['t']), ticker_a, price))
                if no_ticker_a is not None:
                    raw.append((int(pt['t']), no_ticker_a, Decimal('1') - price))
            except (ValueError, TypeError, KeyError):
                continue
        for pt in prices_b:
            try:
                price = Decimal(str(pt['p']))
                raw.append((int(pt['t']), ticker_b, price))
                if no_ticker_b is not None:
                    raw.append((int(pt['t']), no_ticker_b, Decimal('1') - price))
            except (ValueError, TypeError, KeyError):
                continue

        raw.sort(key=lambda x: x[0])

        for ts, ticker, price in raw:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            # Price change event
            self._events.append(
                PriceChangeEvent(ticker=ticker, price=price, timestamp=dt)
            )
            # Synthetic bid
            bid_price = max(price - _HALF_SPREAD, Decimal('0.001'))
            self._events.append(
                OrderBookEvent(
                    ticker=ticker,
                    price=bid_price,
                    size=_BOOK_SIZE,
                    size_delta=_BOOK_SIZE,
                    side='bid',
                )
            )
            # Synthetic ask
            ask_price = min(price + _HALF_SPREAD, Decimal('0.999'))
            self._events.append(
                OrderBookEvent(
                    ticker=ticker,
                    price=ask_price,
                    size=_BOOK_SIZE,
                    size_delta=_BOOK_SIZE,
                    side='ask',
                )
            )

    async def get_next_event(self) -> Event | None:
        if self._idx >= len(self._events):
            return None
        event = self._events[self._idx]
        self._idx += 1
        return event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticker(relation: MarketRelation, leg: str, side: str = 'yes') -> Ticker:
    """Create a PolyMarketTicker or KalshiTicker from a relation leg."""
    m = relation.market_a if leg == 'a' else relation.market_b
    platform = str(m.get('platform', 'polymarket')).lower()
    question = str(m.get('question', m.get('title', '')))[:40]

    if platform == 'kalshi':
        market_ticker = str(m.get('ticker', m.get('id', '')))
        return KalshiTicker(
            symbol=market_ticker,
            name=question,
            market_ticker=market_ticker,
            event_ticker=str(m.get('event_ticker', '')),
            series_ticker=str(m.get('series_ticker', '')),
            side=side,
        )

    if side == 'no':
        token_id = relation.get_no_token_id(leg)
    else:
        token_id = relation.get_token_id(leg)
    market_id = str(m.get('id', ''))
    return PolyMarketTicker(
        symbol=token_id,
        name=question,
        token_id=token_id,
        market_id=market_id,
        event_id=str(m.get('event_id', '')),
        side=side,
    )


def _build_same_event_kwargs(
    kwargs: dict[str, Any],
    relation: MarketRelation,
) -> None:
    """Populate DirectArbStrategy kwargs from a same_event relation.

    Identifies which leg is Polymarket and which is Kalshi, then sets
    poly_market_id, poly_token_id, and kalshi_ticker.
    """
    plat_a = str(relation.market_a.get('platform', 'polymarket')).lower()
    plat_b = str(relation.market_b.get('platform', 'polymarket')).lower()
    if plat_a == 'kalshi':
        poly_m, kalshi_m, poly_leg = relation.market_b, relation.market_a, 'b'
    else:
        poly_m, kalshi_m, poly_leg = relation.market_a, relation.market_b, 'a'
    kwargs.setdefault('poly_market_id', str(poly_m.get('id', '')))
    kwargs.setdefault('poly_token_id', relation.get_token_id(poly_leg))
    kwargs.setdefault(
        'kalshi_ticker',
        str(kalshi_m.get('ticker', kalshi_m.get('id', ''))),
    )


def _build_engine(
    data_source: DataSource,
    strategy: Strategy,
    initial_capital: Decimal,
) -> TradingEngine:
    """Assemble a TradingEngine with PaperTrader for backtesting."""
    market_data = DataManager(
        spread=Decimal('0'),
        max_history_per_ticker=None,
        max_timeline_events=None,
        synthetic_book=False,
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
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )
    return TradingEngine(data_source=data_source, strategy=strategy, trader=trader)


async def _resolve_polymarket_token(
    market: dict[str, Any],
    token_id: str,
) -> str:
    """Ensure token_id is a real CLOB hex token, not a numeric market ID.

    If token_id looks numeric (i.e. the fallback market ID), fetches the
    real token from the Gamma API and updates the market dict in-place.
    """
    if not token_id or not token_id.isdigit():
        return token_id
    # Numeric ID — need to resolve to real CLOB token
    from coinjure.data.fetcher import polymarket_market_info

    info = await polymarket_market_info(token_id)
    if info and info.get('token_ids'):
        market['token_ids'] = info['token_ids']
        return str(info['token_ids'][0])
    return token_id


async def _fetch_leg_prices(
    market: dict[str, Any],
    token_id: str,
) -> list[dict[str, Any]]:
    """Fetch price history for one leg, dispatching by platform."""
    platform = str(market.get('platform', 'polymarket')).lower()

    if platform == 'kalshi':
        from coinjure.data.live.kalshi import fetch_kalshi_price_history

        market_ticker = str(market.get('ticker', market.get('id', '')))
        series_ticker = str(market.get('series_ticker', ''))
        # Derive series_ticker from market_ticker if missing (e.g. KXFEDDECISION-26APR-H0 → KXFEDDECISION)
        if not series_ticker and market_ticker:
            series_ticker = market_ticker.split('-')[0]
        return await fetch_kalshi_price_history(
            series_ticker=series_ticker,
            market_ticker=market_ticker,
        )

    from coinjure.data.live.polymarket import fetch_price_history

    token_id = await _resolve_polymarket_token(market, token_id)
    if not token_id:
        raise ValueError(f'Missing token ID for Polymarket leg')
    return await fetch_price_history(token_id, fidelity=1)


async def _fetch_relation_prices(
    relation: MarketRelation,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch price history for both legs (dispatches by platform)."""
    token_a = relation.get_token_id('a')
    token_b = relation.get_token_id('b')
    prices_a, prices_b = await asyncio.gather(
        _fetch_leg_prices(relation.market_a, token_a),
        _fetch_leg_prices(relation.market_b, token_b),
    )
    return prices_a, prices_b


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_backtest_relation(
    relation: MarketRelation,
    *,
    initial_capital: Decimal = Decimal('10000'),
    parquet_path: str | list[str] | None = None,
    strategy_kwargs: dict[str, Any] | None = None,
) -> BacktestResult:
    """Backtest a relation using its auto-selected strategy.

    For structural types, runs on the full price series.
    For statistical types, walk-forward 60/40: train on 60%, test on 40%.

    Data source is price history from API by default, or parquet if provided.
    """
    from coinjure.strategy.builtin import STRATEGY_BY_RELATION

    spread_type = relation.spread_type
    strategy_cls = STRATEGY_BY_RELATION.get(spread_type)
    if strategy_cls is None:
        return BacktestResult(
            relation_id=relation.relation_id,
            spread_type=spread_type,
            strategy_name='?',
            error=f'No strategy for relation type: {spread_type}',
        )

    kwargs = dict(strategy_kwargs or {})

    # Build strategy-specific kwargs from the relation.
    # DirectArbStrategy (same_event) needs explicit market IDs per platform.
    # GroupArbStrategy (complementary/exclusivity) needs relation_id + event_id.
    # All other strategies take relation_id and load from RelationStore.
    if spread_type == 'same_event':
        _build_same_event_kwargs(kwargs, relation)
        kwargs.setdefault('backtest_mode', True)
    elif spread_type in ('complementary', 'exclusivity'):
        kwargs.setdefault('relation_id', relation.relation_id)
        for m in relation.markets:
            eid = m.get('event_id', '')
            if eid:
                kwargs.setdefault('event_id', str(eid))
                break
    else:
        kwargs.setdefault('relation_id', relation.relation_id)

    result_base = {
        'relation_id': relation.relation_id,
        'spread_type': spread_type,
        'strategy_name': strategy_cls.name or strategy_cls.__name__,
    }

    # --- Resolve data source ---
    if parquet_path is not None:
        from coinjure.data.backtest.parquet import ParquetDataSource

        market_ids = [
            relation.market_a.get('id', ''),
            relation.market_b.get('id', ''),
        ]
        data_source = ParquetDataSource(
            parquet_path,
            market_ids=[m for m in market_ids if m],
        )
        strategy = strategy_cls(**kwargs)
        engine = _build_engine(data_source, strategy, initial_capital)
        await engine.start()
        stats = engine._perf.get_stats()
        pnl = stats.total_pnl
        trades = stats.total_trades
        return BacktestResult(
            **result_base,
            total_pnl=pnl,
            trade_count=trades,
            passed=pnl > 0,
        )

    # --- Price history from API ---
    try:
        prices_a, prices_b = await _fetch_relation_prices(relation)
    except Exception as exc:
        return BacktestResult(**result_base, error=str(exc))

    if len(prices_a) < 10 or len(prices_b) < 10:
        return BacktestResult(
            **result_base,
            error=f'Insufficient price data: A={len(prices_a)}, B={len(prices_b)}',
        )

    ticker_a = _make_ticker(relation, 'a')
    ticker_b = _make_ticker(relation, 'b')

    # Build NO-side tickers if available (needed by EventSumArb etc.)
    no_ticker_a: Ticker | None = None
    no_ticker_b: Ticker | None = None
    if relation.get_no_token_id('a'):
        no_ticker_a = _make_ticker(relation, 'a', side='no')
    if relation.get_no_token_id('b'):
        no_ticker_b = _make_ticker(relation, 'b', side='no')

    if spread_type in STRUCTURAL_TYPES:
        # Structural: run on full data
        ds = PriceHistoryDataSource(
            ticker_a,
            prices_a,
            ticker_b,
            prices_b,
            no_ticker_a=no_ticker_a,
            no_ticker_b=no_ticker_b,
        )
        strategy = strategy_cls(**kwargs)
        engine = _build_engine(ds, strategy, initial_capital)
        await engine.start()
        stats = engine._perf.get_stats()
        return BacktestResult(
            **result_base,
            total_pnl=stats.total_pnl,
            trade_count=stats.total_trades,
            passed=stats.total_pnl > 0,
        )

    # Statistical: walk-forward 60/40
    split_a = int(len(prices_a) * 0.6)
    split_b = int(len(prices_b) * 0.6)
    train_a, test_a = prices_a[:split_a], prices_a[split_a:]
    train_b, test_b = prices_b[:split_b], prices_b[split_b:]

    if len(test_a) < 5 or len(test_b) < 5:
        return BacktestResult(
            **result_base,
            error='Insufficient test data after 60/40 split',
        )

    # Train phase: warm up strategy
    strategy = strategy_cls(**kwargs)
    train_ds = PriceHistoryDataSource(
        ticker_a,
        train_a,
        ticker_b,
        train_b,
        no_ticker_a=no_ticker_a,
        no_ticker_b=no_ticker_b,
    )
    train_engine = _build_engine(train_ds, strategy, initial_capital)
    await train_engine.start()

    # Test phase: reuse calibrated strategy, fresh engine
    test_ds = PriceHistoryDataSource(
        ticker_a,
        test_a,
        ticker_b,
        test_b,
        no_ticker_a=no_ticker_a,
        no_ticker_b=no_ticker_b,
    )
    test_engine = _build_engine(test_ds, strategy, initial_capital)
    await test_engine.start()

    stats = test_engine._perf.get_stats()
    return BacktestResult(
        **result_base,
        total_pnl=stats.total_pnl,
        trade_count=stats.total_trades,
        passed=stats.total_pnl > 0,
    )
