"""Trading engine with snapshot support for live monitoring."""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from swm_agent.analytics.performance_analyzer import PerformanceAnalyzer
from swm_agent.data.data_source import DataSource
from swm_agent.events.events import NewsEvent, OrderBookEvent, PriceChangeEvent
from swm_agent.risk.risk_manager import StandardRiskManager
from swm_agent.strategy.strategy import Strategy
from swm_agent.ticker.ticker import CashTicker
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import OrderStatus

logger = logging.getLogger(__name__)

# Maximum consecutive ``None`` events before the engine warns (continuous mode).
_MAX_CONSECUTIVE_NONE = 120  # ~2 min at 1 s timeout per poll


# ---------------------------------------------------------------------------
# Snapshot data-classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSnapshot:
    ticker_symbol: str
    quantity: Decimal
    average_cost: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    ticker_symbol: str
    bids: List[Tuple[Decimal, Decimal]]  # [(price, size), ...]
    asks: List[Tuple[Decimal, Decimal]]


@dataclass(frozen=True)
class TradeSnapshot:
    time: str
    side: str
    ticker_symbol: str
    price: Decimal
    quantity: Decimal
    status: str


@dataclass(frozen=True)
class OrderSnapshot:
    side: str
    ticker_symbol: str
    limit_price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    status: str


@dataclass(frozen=True)
class EngineSnapshot:
    """Immutable point-in-time copy of the engine / portfolio state."""

    # ---- header metrics ----
    equity: Decimal
    cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    sharpe: Decimal
    max_drawdown_pct: Decimal
    current_drawdown_pct: Decimal
    exposure: Decimal
    exposure_pct: float
    uptime: str
    event_count: int
    engine_running: bool

    # ---- collections ----
    positions: List[PositionSnapshot]
    orderbooks: List[OrderBookSnapshot]
    recent_trades: List[TradeSnapshot]
    active_orders: List[OrderSnapshot]
    news_headlines: List[Tuple[str, str]]  # [(time_str, headline), ...]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TradingEngine:
    def __init__(
        self,
        data_source: DataSource,
        strategy: Strategy,
        trader: Trader,
        *,
        initial_capital: Decimal = Decimal('10000'),
        continuous: bool = False,
    ) -> None:
        self.data_source = data_source
        self.strategy = strategy
        self.trader = trader
        self.market_data = trader.market_data
        self.running = False

        # When *continuous* is True the engine keeps looping even when
        # ``get_next_event()`` returns ``None`` (live data sources use
        # ``None`` to signal "nothing right now", not "data exhausted").
        self._continuous = continuous

        # --- monitoring helpers ---
        self._start_time: Optional[datetime] = None
        self._event_count: int = 0
        self._last_orders_idx: int = 0
        self._order_times: list[str] = []
        self._perf = PerformanceAnalyzer(initial_capital=initial_capital)
        self._news: deque[Tuple[str, str]] = deque(maxlen=100)

        # [H3] Guard: prevent calling data_source.start() more than once.
        self._ds_started: bool = False

    # ------------------------------------------------------------------ #
    # Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._start_time = datetime.now()
        self.running = True

        # [H3] Start the data source exactly once.
        if not self._ds_started:
            self._ds_started = True
            await self.data_source.start()

        logger.info('TradingEngine started (continuous=%s)', self._continuous)

        # [M1] Track consecutive None events so we can warn on prolonged
        # silence instead of silently spinning.
        consecutive_none = 0

        while self.running:
            try:
                event = await self.data_source.get_next_event()
            except Exception:
                logger.exception('Error fetching next event')
                # [C1] Back off on repeated fetch errors to avoid CPU spin.
                await asyncio.sleep(1.0)
                continue

            if event is None:
                if self._continuous:
                    consecutive_none += 1
                    # [M1] Warn periodically on prolonged silence.
                    if consecutive_none == _MAX_CONSECUTIVE_NONE:
                        logger.warning(
                            'No events received for %d consecutive polls — '
                            'data source may be disconnected',
                            consecutive_none,
                        )
                    elif (
                        consecutive_none > _MAX_CONSECUTIVE_NONE
                        and consecutive_none % _MAX_CONSECUTIVE_NONE == 0
                    ):
                        logger.warning(
                            'Still no events (%d consecutive Nones)',
                            consecutive_none,
                        )
                    continue
                self.running = False
                logger.info('Data source exhausted — engine stopping')
                break

            # Got a real event — reset the silence counter.
            consecutive_none = 0
            self._event_count += 1

            try:
                if isinstance(event, OrderBookEvent):
                    self.market_data.process_orderbook_event(event)
                elif isinstance(event, PriceChangeEvent):
                    self.market_data.process_price_change_event(event)
                    logger.debug(
                        'Price %s → %s', event.ticker.symbol, event.price
                    )

                if isinstance(event, NewsEvent):
                    headline = event.title or event.news[:100]
                    self._news.append(
                        (datetime.now().strftime('%H:%M:%S'), headline)
                    )
                    logger.info('NewsEvent: %s', headline)

                await self.strategy.process_event(event, self.trader)
                self._sync_trades()
            except Exception:
                logger.exception('Error processing event #%d', self._event_count)

        logger.info('TradingEngine stopped  (events=%d)', self._event_count)

    async def stop(self) -> None:
        """Stop the engine **and** its data source (async-safe)."""
        self.running = False
        # [C2] Propagate shutdown to the data source so it can cancel
        # any background polling tasks it may have launched.
        try:
            await self.data_source.stop()
        except Exception:
            logger.debug('data_source.stop() error (ignored)', exc_info=True)

    def request_stop(self) -> None:
        """Non-async flag-flip — safe to call from any context."""
        self.running = False

    # ------------------------------------------------------------------ #
    # Trade tracking                                                      #
    # ------------------------------------------------------------------ #

    def _sync_trades(self) -> None:
        """Feed newly-appeared orders into the performance analyser."""
        # [M3] Now that Trader base-class declares ``orders``, we can
        # access it directly instead of using getattr().
        orders = self.trader.orders
        now_str = datetime.now().strftime('%H:%M:%S')
        while self._last_orders_idx < len(orders):
            order = orders[self._last_orders_idx]
            self._order_times.append(now_str)
            for trade in order.trades:
                self._perf.add_trade(trade)
            self._last_orders_idx += 1

    # ------------------------------------------------------------------ #
    # Snapshot (non-blocking, no awaits)                                  #
    # ------------------------------------------------------------------ #

    def get_snapshot(self) -> EngineSnapshot:
        """Return an immutable snapshot of current engine + portfolio state.

        This method is intentionally **synchronous** and never yields, so it
        is safe to call from the UI refresh loop between ``await`` points.
        All mutable collections (``order_books``, ``orders``, …) are
        copied via ``list()`` to avoid issues if a background task mutates
        them concurrently.
        """
        pm = self.trader.position_manager
        md = self.trader.market_data
        rm = self.trader.risk_manager

        # ---- equity / pnl -----------------------------------------------
        realized_pnl = pm.get_total_realized_pnl()
        unrealized_pnl = Decimal('0')
        cash = Decimal('0')
        equity = Decimal('0')

        for cp in pm.get_cash_positions():
            cash += cp.quantity

        for pos in pm.get_non_cash_positions():
            try:
                bid = md.get_best_bid(pos.ticker)
                if bid is not None:
                    unrealized_pnl += (bid.price - pos.average_cost) * pos.quantity
            except (KeyError, AttributeError):
                pass

        try:
            equity = sum(pm.get_portfolio_value(md).values(), Decimal('0'))
        except (KeyError, AttributeError):
            equity = cash

        total_pnl = realized_pnl + unrealized_pnl

        # ---- sharpe / drawdown from perf analyser -----------------------
        stats = self._perf.get_stats()
        sharpe = stats.sharpe_ratio
        max_dd = stats.max_drawdown

        # ---- current drawdown from risk manager -------------------------
        current_dd = Decimal('0')
        if isinstance(rm, StandardRiskManager):
            try:
                current_dd = rm.get_current_drawdown()
            except Exception:
                pass

        # ---- exposure ---------------------------------------------------
        market_value = max(equity - cash, Decimal('0'))
        exposure_pct = (
            float(market_value / equity * 100) if equity > 0 else 0.0
        )

        # ---- positions --------------------------------------------------
        pos_snaps: List[PositionSnapshot] = []
        for pos in pm.get_non_cash_positions():
            if pos.quantity == 0:
                continue
            cur_price = Decimal('0')
            u_pnl = Decimal('0')
            try:
                bid = md.get_best_bid(pos.ticker)
                if bid is not None:
                    cur_price = bid.price
                    u_pnl = (cur_price - pos.average_cost) * pos.quantity
            except (KeyError, AttributeError):
                pass
            pos_snaps.append(
                PositionSnapshot(
                    ticker_symbol=pos.ticker.symbol,
                    quantity=pos.quantity,
                    average_cost=pos.average_cost,
                    current_price=cur_price,
                    unrealized_pnl=u_pnl,
                    realized_pnl=pos.realized_pnl,
                )
            )

        # ---- order books ------------------------------------------------
        # [H1] Snapshot the dict to avoid RuntimeError if it's mutated.
        # [M2] Sort by symbol for stable UI ordering.
        ob_snaps: List[OrderBookSnapshot] = []
        for ticker, ob in sorted(
            list(md.order_books.items()),
            key=lambda kv: kv[0].symbol,
        ):
            if isinstance(ticker, CashTicker):
                continue
            bids = [(lv.price, lv.size) for lv in ob.get_bids(5)]
            asks = [(lv.price, lv.size) for lv in ob.get_asks(5)]
            if bids or asks:
                ob_snaps.append(
                    OrderBookSnapshot(
                        ticker_symbol=ticker.symbol, bids=bids, asks=asks,
                    )
                )

        # ---- recent trades / active orders ------------------------------
        # [M3] Direct attribute access now that Trader declares ``orders``.
        all_orders = list(self.trader.orders)  # snapshot the list

        trade_snaps: List[TradeSnapshot] = []
        active_snaps: List[OrderSnapshot] = []

        for idx in range(len(all_orders) - 1, max(len(all_orders) - 20, -1) - 1, -1):
            if idx < 0:
                break
            order = all_orders[idx]
            side_str = order.side.value.upper()
            status_str = order.status.value.upper()
            ts = (
                self._order_times[idx]
                if idx < len(self._order_times)
                else ''
            )

            if order.status in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                trade_snaps.append(
                    TradeSnapshot(
                        time=ts,
                        side=side_str,
                        ticker_symbol=order.ticker.symbol,
                        price=(
                            order.average_price
                            if order.average_price > 0
                            else order.limit_price
                        ),
                        quantity=order.filled_quantity,
                        status=status_str,
                    )
                )
            if order.status == OrderStatus.PLACED:
                active_snaps.append(
                    OrderSnapshot(
                        side=side_str,
                        ticker_symbol=order.ticker.symbol,
                        limit_price=order.limit_price,
                        quantity=order.remaining + order.filled_quantity,
                        filled_quantity=order.filled_quantity,
                        status=status_str,
                    )
                )

        # ---- uptime -----------------------------------------------------
        uptime = '00:00:00'
        if self._start_time:
            secs = int((datetime.now() - self._start_time).total_seconds())
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            uptime = f'{h:02d}:{m:02d}:{s:02d}'

        return EngineSnapshot(
            equity=equity,
            cash=cash,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            sharpe=sharpe,
            max_drawdown_pct=max_dd,
            current_drawdown_pct=current_dd,
            exposure=market_value,
            exposure_pct=exposure_pct,
            uptime=uptime,
            event_count=self._event_count,
            engine_running=self.running,
            positions=pos_snaps,
            orderbooks=ob_snaps,
            recent_trades=trade_snaps[:10],
            active_orders=active_snaps,
            news_headlines=list(self._news),
        )
