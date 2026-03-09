"""Backtest engine — runs strategies against historical data for market relations.

Data sources:
  - Price history from CLOB API (default, lower granularity)
  - Parquet orderbook files (optional, higher granularity)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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

# Synthetic half-spread for generating bid/ask from mid price.
# Zero by default: backtest assumes no slippage. Set > 0 to simulate spread.
_HALF_SPREAD = Decimal('0')
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

    Accepts N price series (one per leg) and interleaves them by timestamp.
    Generates synthetic bid/ask OrderBookEvents so PaperTrader has liquidity.

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
        legs = [
            (ticker_a, prices_a, no_ticker_a),
            (ticker_b, prices_b, no_ticker_b),
        ]
        self._events: list[Event] = []
        self._idx = 0
        self._build_events(legs)

    @classmethod
    def from_legs(
        cls,
        legs: list[tuple[Ticker, list[dict[str, Any]], Ticker | None]],
    ) -> PriceHistoryDataSource:
        """Create from N legs, each (yes_ticker, prices, no_ticker)."""
        instance = object.__new__(cls)
        instance._events = []
        instance._idx = 0
        instance._build_events(legs)
        return instance

    def _build_events(
        self,
        legs: list[tuple[Ticker, list[dict[str, Any]], Ticker | None]],
    ) -> None:
        """Convert raw {t, p} points into engine events, sorted by timestamp."""
        raw: list[tuple[int, Ticker, Decimal]] = []
        for yes_ticker, prices, no_ticker in legs:
            for pt in prices:
                try:
                    price = Decimal(str(pt['p']))
                    raw.append((int(pt['t']), yes_ticker, price))
                    if no_ticker is not None:
                        raw.append((int(pt['t']), no_ticker, Decimal('1') - price))
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


def _make_ticker(relation: MarketRelation, index: int, side: str = 'yes') -> Ticker:
    """Create a PolyMarketTicker or KalshiTicker from a relation market."""
    m = relation.markets[index]
    platform = str(m.get('platform', 'polymarket')).lower()
    question = str(m.get('question', m.get('title', '')))[:40]

    if platform == 'kalshi':
        market_ticker = str(m.get('ticker', m.get('id', '')))
        # Differentiate YES/NO symbols to avoid PositionManager collision
        symbol = f'{market_ticker}:{side}' if side == 'no' else market_ticker
        return KalshiTicker(
            symbol=symbol,
            name=question,
            market_ticker=market_ticker,
            event_ticker=str(m.get('event_ticker', '')),
            series_ticker=str(m.get('series_ticker', '')),
            side=side,
        )

    if side == 'no':
        token_id = relation.get_no_token_id(index)
    else:
        token_id = relation.get_token_id(index)
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
    m0 = relation.markets[0] if relation.markets else {}
    m1 = relation.markets[1] if len(relation.markets) > 1 else {}
    plat_0 = str(m0.get('platform', 'polymarket')).lower()
    if plat_0 == 'kalshi':
        poly_m, kalshi_m, poly_idx = m1, m0, 1
    else:
        poly_m, kalshi_m, poly_idx = m0, m1, 0
    kwargs.setdefault('poly_market_id', str(poly_m.get('id', '')))
    kwargs.setdefault('poly_token_id', relation.get_token_id(poly_idx))
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
    # Fund Kalshi USD too so cross-platform arbs (same_event) can trade both legs
    position_manager.update_position(
        Position(
            ticker=CashTicker.KALSHI_USD,
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
    return TradingEngine(data_source=data_source, strategy=strategy, trader=trader)


def _engine_total_pnl(
    engine: TradingEngine, initial_capital: Decimal = Decimal('10000')
) -> tuple[Decimal, int]:
    """Return (total_pnl, trade_count) as ending equity minus initial capital.

    Computes actual equity: cash remaining + market value of all open positions.
    This correctly handles buy-only strategies (structural arbs) where
    PerformanceAnalyzer only sees negative cash flows from BUY trades.

    initial_capital is the amount funded PER cash ticker (both Poly USDC
    and Kalshi USD get this amount).  PnL = total_equity - total_funded.
    """
    pm = engine.trader.position_manager
    md = engine.trader.market_data
    total_equity = Decimal('0')
    cash_tickers_count = 0

    for pos in pm.positions.values():
        if isinstance(pos.ticker, CashTicker):
            total_equity += pos.quantity
            cash_tickers_count += 1
        elif pos.quantity > 0:
            # Mark to current market price
            current_price = Decimal('0')
            best_bid = md.get_best_bid(pos.ticker)
            if best_bid is not None:
                current_price = best_bid.price
            else:
                best_ask = md.get_best_ask(pos.ticker)
                if best_ask is not None:
                    current_price = best_ask.price
            if current_price > 0:
                total_equity += current_price * pos.quantity
            else:
                # No price data — fall back to cost basis (conservative)
                total_equity += pos.average_cost * pos.quantity

    total_funded = initial_capital * max(cash_tickers_count, 1)
    pnl = total_equity - total_funded
    trade_count = len(engine._perf.trades)
    return pnl, trade_count


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
        # Disable cooldown — time.monotonic() doesn't advance in backtest
        kwargs.setdefault('cooldown_seconds', 0)
    elif spread_type in ('complementary', 'exclusivity'):
        kwargs.setdefault('relation_id', relation.relation_id)
        # Disable cooldown — time.monotonic() doesn't advance in backtest
        kwargs.setdefault('cooldown_seconds', 0)
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

        market_ids = [m.get('id', '') for m in relation.markets]
        data_source = ParquetDataSource(
            parquet_path,
            market_ids=[m for m in market_ids if m],
        )
        strategy = strategy_cls(**kwargs)
        engine = _build_engine(data_source, strategy, initial_capital)
        await engine.start()
        pnl, trades = _engine_total_pnl(engine, initial_capital)
        return BacktestResult(
            **result_base,
            total_pnl=pnl,
            trade_count=trades,
            passed=pnl > 0,
        )

    # --- Price history from API ---
    # Fetch prices for ALL legs sequentially to respect API rate limits
    # (Kalshi rate-limits aggressively). The engine run phase is CPU-only.
    n_markets = len(relation.markets)
    _KALSHI_DELAY = 3.0  # seconds between Kalshi API calls

    try:
        all_prices: list[list[dict[str, Any]]] = []
        for i in range(n_markets):
            m = relation.markets[i]
            is_kalshi = str(m.get('platform', 'polymarket')).lower() == 'kalshi'
            if is_kalshi and all_prices:
                await asyncio.sleep(_KALSHI_DELAY)
            prices = await _fetch_leg_prices(m, relation.get_token_id(i))
            all_prices.append(prices)
    except Exception as exc:
        return BacktestResult(**result_base, error=str(exc))

    # Build legs: (yes_ticker, prices, no_ticker) for each market with data
    legs: list[tuple[Ticker, list[dict[str, Any]], Ticker | None]] = []
    for i, prices in enumerate(all_prices):
        if len(prices) < 10:
            continue
        yes_ticker = _make_ticker(relation, i)
        no_tid = relation.get_no_token_id(i)
        no_ticker = _make_ticker(relation, i, side='no') if no_tid else None
        legs.append((yes_ticker, prices, no_ticker))

    if len(legs) < 2:
        data_lens = ', '.join(f'{i}={len(p)}' for i, p in enumerate(all_prices))
        return BacktestResult(
            **result_base,
            error=f'Insufficient price data: {data_lens}',
        )

    if spread_type in STRUCTURAL_TYPES:
        # Structural: run on full data
        ds = PriceHistoryDataSource.from_legs(legs)
        strategy = strategy_cls(**kwargs)
        engine = _build_engine(ds, strategy, initial_capital)
        await engine.start()
        pnl, trades = _engine_total_pnl(engine, initial_capital)
        return BacktestResult(
            **result_base,
            total_pnl=pnl,
            trade_count=trades,
            passed=pnl > 0,
        )

    # Statistical: walk-forward 60/40
    train_legs: list[tuple[Ticker, list[dict[str, Any]], Ticker | None]] = []
    test_legs: list[tuple[Ticker, list[dict[str, Any]], Ticker | None]] = []
    for yes_ticker, prices, no_ticker in legs:
        split = int(len(prices) * 0.6)
        train_legs.append((yes_ticker, prices[:split], no_ticker))
        test_legs.append((yes_ticker, prices[split:], no_ticker))

    if any(len(p) < 5 for _, p, _ in test_legs):
        return BacktestResult(
            **result_base,
            error='Insufficient test data after 60/40 split',
        )

    # Train phase: warm up strategy
    strategy = strategy_cls(**kwargs)
    train_ds = PriceHistoryDataSource.from_legs(train_legs)
    train_engine = _build_engine(train_ds, strategy, initial_capital)
    await train_engine.start()

    # Test phase: reuse calibrated strategy, fresh engine
    strategy.reset_live_state()
    test_ds = PriceHistoryDataSource.from_legs(test_legs)
    test_engine = _build_engine(test_ds, strategy, initial_capital)
    await test_engine.start()

    pnl, trades = _engine_total_pnl(test_engine, initial_capital)
    return BacktestResult(
        **result_base,
        total_pnl=pnl,
        trade_count=trades,
        passed=pnl > 0,
    )
