"""Multi-strategy engine: single process, shared DataSource + DataManager, N strategy-trader slots.

Replaces the N-subprocess model in ``_run_batch`` with a single event loop
that fans out each market event to every active strategy slot.  This cuts
CPU usage from O(N) processes to O(1) while preserving per-strategy isolation
for positions, risk, and decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from coinjure.data.manager import DataManager
from coinjure.data.source import DataSource
from coinjure.engine.performance import PerformanceAnalyzer
from coinjure.events import NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.trading.risk import StandardRiskManager
from coinjure.trading.types import OrderStatus

if TYPE_CHECKING:
    from coinjure.engine.trader.alerter import Alerter
    from coinjure.storage.state_store import StateStore
    from coinjure.strategy.strategy import Strategy
    from coinjure.ticker import Ticker
    from coinjure.trading.trader import Trader

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_NONE = 120
_SAVE_INTERVAL = 100
_PRUNE_INTERVAL = 500
_MAX_CONSECUTIVE_ERRORS = 5
_LLM_REVIEW_INTERVAL = 500


# ── Slot (per-strategy state) ─────────────────────────────────────────────


@dataclass
class EngineSlot:
    """One strategy-trader pair inside a MultiStrategyEngine."""

    slot_id: str
    strategy: Strategy
    trader: Trader
    perf: PerformanceAnalyzer
    state_store: StateStore | None = None
    alerter: Alerter | None = None
    drawdown_alert_pct: Decimal | None = None

    # ── runtime state ──
    paused: bool = False
    degraded_read_only: bool = False
    event_count: int = 0
    save_counter: int = 0
    last_orders_idx: int = 0
    last_trade_count: int = 0
    last_decisions_count: int = 0
    consecutive_errors: int = 0
    drawdown_alerted: bool = False
    llm_review_counter: int = 0

    activity_log: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=100)
    )
    news: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=300))
    order_times: list[str] = field(default_factory=list)


# ── MultiStrategyEngine ───────────────────────────────────────────────────────────


class MultiStrategyEngine:
    """Single-process engine running N strategy-trader slots against shared market data.

    Architecture
    ────────────
    - ONE DataSource (HubDataSource + RSS, or CompositeDataSource)
    - ONE DataManager (shared order books — written once per event, read by all slots)
    - N EngineSlot instances, each with its own Strategy + Trader + PositionManager
    - ONE asyncio event loop

    The main loop fetches an event, applies it to the shared DataManager,
    then fans out to every active (non-paused) slot for strategy processing.
    """

    def __init__(
        self,
        data_source: DataSource,
        market_data: DataManager,
        slots: list[EngineSlot],
        *,
        continuous: bool = True,
    ) -> None:
        self.data_source = data_source
        self.market_data = market_data
        self.slots = {s.slot_id: s for s in slots}
        self.running = False
        self._continuous = continuous
        self._ds_started = False
        self._start_time: datetime | None = None
        self._global_event_count = 0
        self._last_prune_event = 0
        self._deferred_news: deque[NewsEvent] = deque(maxlen=500)

    # ── Slot lookup ────────────────────────────────────────────────────

    def get_slot(self, slot_id: str) -> EngineSlot | None:
        return self.slots.get(slot_id)

    @property
    def active_slots(self) -> list[EngineSlot]:
        return [s for s in self.slots.values() if not s.paused]

    # ── Market data (shared) ───────────────────────────────────────────

    def _apply_market_event(self, event: object) -> None:
        if isinstance(event, OrderBookEvent):
            self.market_data.process_orderbook_event(event)
        elif isinstance(event, PriceChangeEvent):
            self.market_data.process_price_change_event(event)
            logger.debug('Price %s -> %s', event.ticker.symbol, event.price)
        # Try filling resting orders for ALL traders
        for slot in self.slots.values():
            if hasattr(slot.trader, 'try_fill_resting_orders'):
                slot.trader.try_fill_resting_orders()

    def _drain_batch(self, event: object) -> list[object]:
        """Drain pending events from the data source (live mode)."""
        if self._continuous:
            batch = [event]
            drain_queue = getattr(self.data_source, 'drain_pending_events', None)
            if callable(drain_queue):
                batch.extend(drain_queue())
            return batch
        return [event]

    # ── Per-slot event processing ──────────────────────────────────────

    async def _process_slot_event(
        self,
        slot: EngineSlot,
        event: object,
    ) -> None:
        """Process a single event for one slot (strategy + trade sync)."""
        slot.event_count += 1
        now_str = datetime.now().strftime('%H:%M:%S')

        try:
            # News logging
            if isinstance(event, NewsEvent):
                headline = event.title or event.news[:100]
                source = getattr(event, 'source', '') or ''
                url = getattr(event, 'url', '') or ''
                slot.trader.record_news(
                    timestamp=now_str, title=headline, source=source, url=url
                )
                slot.news.append(
                    {
                        'timestamp': now_str,
                        'title': headline,
                        'source': source,
                        'url': url,
                    }
                )

            # Strategy execution
            prev_orders = len(slot.trader.orders)
            slot.strategy.bind_context(event, slot.trader)
            await slot.strategy.process_event(event, slot.trader)

            # Record decisions
            decisions = slot.strategy.get_decisions()
            stats = slot.strategy.get_decision_stats()
            total_d = int(stats.get('decisions', len(decisions)))
            if total_d > slot.last_decisions_count:
                new_count = total_d - slot.last_decisions_count
                start_idx = max(0, len(decisions) - new_count)
                for i in range(start_idx, len(decisions)):
                    d = decisions[i]
                    exec_mark = 'TRADED' if d.executed else 'no trade'
                    signals = d.signal_values or {}
                    signal_pairs = list(signals.items())[:2]
                    signal_str = (
                        ' '.join(f'{k}={v:.3f}' for k, v in signal_pairs)
                        if signal_pairs
                        else ''
                    )
                    name = d.ticker_name[:30]
                    slot.activity_log.append(
                        (now_str, f'{d.action} {signal_str} [{exec_mark}] "{name}"')
                    )
                slot.last_decisions_count = total_d

            # Trade sync
            await self._sync_slot_trades(slot)

            # Log new orders
            new_orders = slot.trader.orders[prev_orders:]
            for order in new_orders:
                status = order.status.value.upper()
                side = order.side.value.upper()
                ticker_name = (
                    getattr(order.ticker, 'name', '') or order.ticker.symbol[:25]
                )
                slot.activity_log.append(
                    (
                        now_str,
                        f'{side} {order.filled_quantity} @ ${order.average_price:.4f} -> {status} "{ticker_name[:25]}"',
                    )
                )
                # Watch/unwatch tokens on trade
                if hasattr(order.ticker, 'token_id'):
                    if order.side.value.upper() == 'BUY':
                        watch = getattr(self.data_source, 'watch_token', None)
                        if watch:
                            watch(order.ticker.token_id)
                            complement = self.market_data.find_complement(order.ticker)
                            if complement and hasattr(complement, 'token_id'):
                                watch(complement.token_id)
                    elif order.side.value.upper() == 'SELL':
                        pos = slot.trader.position_manager.get_position(order.ticker)
                        if pos is None or pos.quantity <= 0:
                            unwatch = getattr(self.data_source, 'unwatch_token', None)
                            if unwatch:
                                unwatch(order.ticker.token_id)
        except Exception as exc:
            slot.activity_log.append(
                (now_str, f'Error processing event #{slot.event_count}')
            )
            logger.exception(
                '[%s] Error processing event #%d', slot.slot_id, slot.event_count
            )
            slot.consecutive_errors += 1
            if slot.alerter:
                try:
                    await slot.alerter.on_error(exc)
                except Exception:
                    pass
            await self._auto_degrade_slot(slot)
        else:
            slot.consecutive_errors = 0

        # Periodic save
        slot.save_counter += 1
        if slot.save_counter >= _SAVE_INTERVAL:
            slot.save_counter = 0
            if slot.state_store:
                try:
                    slot.state_store.save_all(slot.trader.position_manager, slot.perf)
                except Exception:
                    logger.debug(
                        '[%s] state_store.save_all() failed',
                        slot.slot_id,
                        exc_info=True,
                    )
            await self._check_drawdown_alert(slot)
            await self._check_portfolio_health(slot)

        # Periodic LLM review
        slot.llm_review_counter += 1
        if slot.llm_review_counter >= _LLM_REVIEW_INTERVAL:
            slot.llm_review_counter = 0
            await self._check_llm_portfolio_review(slot)

    async def _sync_slot_trades(self, slot: EngineSlot) -> None:
        """Feed new orders/trades into the performance analyser for one slot."""
        orders = slot.trader.orders
        now_str = datetime.now().strftime('%H:%M:%S')

        while slot.last_orders_idx < len(orders):
            order = orders[slot.last_orders_idx]
            slot.order_times.append(now_str)
            if slot.state_store:
                try:
                    slot.state_store.append_order(order)
                except Exception:
                    pass
            if slot.alerter and order.status not in (
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
            ):
                try:
                    await slot.alerter.on_order_placed(order)
                except Exception:
                    pass
            slot.last_orders_idx += 1

        total_trades = sum(len(o.trades) for o in orders)
        if total_trades > slot.last_trade_count:
            seen = 0
            for order in orders:
                for trade in order.trades:
                    seen += 1
                    if seen > slot.last_trade_count:
                        slot.perf.add_trade(trade)
                        if slot.state_store:
                            try:
                                slot.state_store.append_trade(trade)
                            except Exception:
                                pass
                        if slot.alerter and order.status in (
                            OrderStatus.FILLED,
                            OrderStatus.PARTIALLY_FILLED,
                        ):
                            try:
                                await slot.alerter.on_trade(trade)
                            except Exception:
                                pass
            slot.last_trade_count = total_trades

    async def _check_drawdown_alert(self, slot: EngineSlot) -> None:
        if not slot.alerter or slot.drawdown_alert_pct is None:
            return
        rm = slot.trader.risk_manager
        if not isinstance(rm, StandardRiskManager):
            return
        try:
            current_dd = rm.get_current_drawdown()
            if current_dd >= slot.drawdown_alert_pct and not slot.drawdown_alerted:
                slot.drawdown_alerted = True
                await slot.alerter.on_drawdown_alert(
                    current_dd, slot.drawdown_alert_pct
                )
            elif current_dd < slot.drawdown_alert_pct:
                slot.drawdown_alerted = False
        except Exception:
            pass

    async def _check_portfolio_health(self, slot: EngineSlot) -> None:
        rm = slot.trader.risk_manager
        if not isinstance(rm, StandardRiskManager):
            return
        try:
            ok, reason = rm.check_portfolio_health()
        except Exception:
            return
        if ok or slot.degraded_read_only:
            return
        now_str = datetime.now().strftime('%H:%M:%S')
        slot.activity_log.append((now_str, f'Risk breach: {reason} -> READ-ONLY'))
        logger.error('[%s] Risk breach: %s -> READ-ONLY', slot.slot_id, reason)
        slot.trader.set_read_only(True)
        slot.strategy.set_paused(True)
        slot.degraded_read_only = True
        if slot.alerter:
            try:
                await slot.alerter.on_risk_limit_hit(reason)
            except Exception:
                pass

    async def _check_llm_portfolio_review(self, slot: EngineSlot) -> None:
        llm_portfolio_review = getattr(slot.strategy, 'llm_portfolio_review', False)
        if not llm_portfolio_review:
            return
        llm_model = getattr(slot.strategy, 'llm_model', None)
        kelly_fraction = getattr(slot.strategy, 'kelly_fraction', None)
        max_trade_size = getattr(slot.strategy, 'max_trade_size', None)
        if kelly_fraction is None or max_trade_size is None:
            return
        pm = slot.trader.position_manager
        available = Decimal('0')
        for pos in pm.get_cash_positions():
            available += pos.quantity
        exposure = Decimal('0')
        realized_pnl = Decimal('0')
        for pos in pm.get_non_cash_positions():
            if pos.quantity > 0:
                exposure += pos.quantity * pos.average_cost
            realized_pnl += pos.realized_pnl
        unrealized_pnl = pm.get_total_unrealized_pnl(self.market_data)
        strategy_id = getattr(slot.strategy, 'relation_id', None) or slot.slot_id
        trade_count = len(slot.trader.orders)
        try:
            from coinjure.trading.llm_allocator import review_portfolio_llm

            adjustment = await review_portfolio_llm(
                strategy_id=strategy_id,
                available_capital=available,
                current_exposure=exposure,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                position_count=len(pm.get_non_cash_positions()),
                kelly_fraction=kelly_fraction,
                max_trade_size=max_trade_size,
                trade_count=trade_count,
                model=llm_model or 'gpt-4.1-mini',
            )
        except Exception:
            logger.debug(
                '[%s] LLM portfolio review failed', slot.slot_id, exc_info=True
            )
            return
        if adjustment is None:
            return
        if adjustment.kelly_fraction is not None:
            old = slot.strategy.kelly_fraction
            slot.strategy.kelly_fraction = adjustment.kelly_fraction
            logger.info(
                '[%s] LLM adjusted kelly_fraction: %s -> %s (%s)',
                slot.slot_id,
                old,
                adjustment.kelly_fraction,
                adjustment.reasoning,
            )
        if adjustment.max_trade_size is not None:
            old = slot.strategy.max_trade_size
            slot.strategy.max_trade_size = adjustment.max_trade_size
            logger.info(
                '[%s] LLM adjusted max_trade_size: %s -> %s (%s)',
                slot.slot_id,
                old,
                adjustment.max_trade_size,
                adjustment.reasoning,
            )

    async def _auto_degrade_slot(self, slot: EngineSlot) -> None:
        if slot.consecutive_errors < _MAX_CONSECUTIVE_ERRORS:
            return
        if slot.degraded_read_only:
            return
        msg = (
            f'[{slot.slot_id}] Consecutive errors={slot.consecutive_errors} '
            '-> auto-degrade READ-ONLY'
        )
        now_str = datetime.now().strftime('%H:%M:%S')
        slot.activity_log.append((now_str, msg))
        logger.error(msg)
        slot.trader.set_read_only(True)
        slot.strategy.set_paused(True)
        slot.degraded_read_only = True
        if slot.alerter:
            try:
                await slot.alerter.on_risk_limit_hit('auto_degrade_error_storm')
            except Exception:
                pass

    # ── Main loop ──────────────────────────────────────────────────────

    async def start(self) -> None:  # noqa: C901
        self._start_time = datetime.now()
        self.running = True

        # Start data source once
        if not self._ds_started:
            self._ds_started = True
            await self.data_source.start()

        # Register watch tokens from ALL strategies
        watch = getattr(self.data_source, 'watch_token', None)
        register = getattr(self.data_source, 'register_token_ticker', None)
        for slot in self.slots.values():
            if watch:
                if register:
                    for attr in ('_yes_tickers', '_no_tickers'):
                        tickers = getattr(slot.strategy, attr, {})
                        for tid, ticker in tickers.items():
                            register(tid, ticker)
                for token_id in slot.strategy.watch_tokens():
                    watch(token_id)
            try:
                await slot.strategy.on_start()
            except Exception:
                logger.debug(
                    '[%s] strategy.on_start() failed', slot.slot_id, exc_info=True
                )

        n_slots = len(self.slots)
        logger.info(
            'MultiStrategyEngine started: %d slots, continuous=%s',
            n_slots,
            self._continuous,
        )

        consecutive_none = 0
        _fetch_backoff: float = 1.0
        _FETCH_BACKOFF_CAP: float = 30.0
        _consecutive_fetch_failures: int = 0
        _MAX_FETCH_FAILURES: int = 10

        while self.running:
            try:
                event = await self.data_source.get_next_event()
            except Exception:
                _consecutive_fetch_failures += 1
                logger.exception(
                    'Error fetching next event (attempt %d, backoff %.1fs)',
                    _consecutive_fetch_failures,
                    _fetch_backoff,
                )
                await asyncio.sleep(_fetch_backoff)
                _fetch_backoff = min(_fetch_backoff * 2, _FETCH_BACKOFF_CAP)
                if _consecutive_fetch_failures >= _MAX_FETCH_FAILURES:
                    logger.error(
                        '%d consecutive data-fetch failures — freezing all slots',
                        _consecutive_fetch_failures,
                    )
                    for slot in self.slots.values():
                        slot.trader.set_read_only(True)
                        slot.strategy.set_paused(True)
                        slot.degraded_read_only = True
                continue

            if _consecutive_fetch_failures > 0:
                logger.info(
                    'Data fetch recovered after %d failures',
                    _consecutive_fetch_failures,
                )
            _fetch_backoff = 1.0
            _consecutive_fetch_failures = 0

            if event is None:
                if self._deferred_news:
                    event = self._deferred_news.popleft()
                elif self._continuous:
                    consecutive_none += 1
                    if consecutive_none == _MAX_CONSECUTIVE_NONE:
                        logger.warning(
                            'No events received for %d consecutive polls',
                            consecutive_none,
                        )
                    continue
                else:
                    self.running = False
                    break

            consecutive_none = 0

            # Drain market events before processing a NewsEvent
            # Collect PriceChangeEvents so they still reach strategies.
            _drained_price: list[object] = []
            if self._continuous and isinstance(event, NewsEvent):
                drained = 0
                while True:
                    try:
                        peek = await asyncio.wait_for(
                            self.data_source.get_next_event(), timeout=0.05
                        )
                    except (asyncio.TimeoutError, Exception):
                        break
                    if peek is None:
                        break
                    self._global_event_count += 1
                    if isinstance(peek, OrderBookEvent):
                        self.market_data.process_orderbook_event(peek)
                        drained += 1
                    elif isinstance(peek, PriceChangeEvent):
                        self.market_data.process_price_change_event(peek)
                        _drained_price.append(peek)
                        drained += 1
                    elif isinstance(peek, NewsEvent):
                        self._deferred_news.append(peek)

            # Drain batch and apply to shared market data ONCE
            batch = self._drain_batch(event)
            if _drained_price:
                batch.extend(_drained_price)
            if len(batch) > 1:
                for b_event in batch:
                    self._apply_market_event(b_event)

            # Fan out to all active slots
            for b_event in batch:
                self._global_event_count += 1
                if len(batch) == 1:
                    self._apply_market_event(b_event)

                for slot in list(self.slots.values()):
                    if slot.paused:
                        continue
                    await self._process_slot_event(slot, b_event)

                # Yield periodically
                if self._global_event_count & 63 == 63:
                    await asyncio.sleep(0)

            # Yield at end of each main-loop iteration
            await asyncio.sleep(0)

            # Periodic stale ticker pruning (shared)
            if self._global_event_count - self._last_prune_event >= _PRUNE_INTERVAL:
                self._last_prune_event = self._global_event_count
                known = getattr(self.data_source, '_known_tickers', None)
                if known is None:
                    for src in getattr(self.data_source, 'sources', []):
                        known = getattr(src, '_known_tickers', None)
                        if known is not None:
                            break
                if known is not None:
                    removed = self.market_data.prune_stale_tickers(set(known.keys()))
                    if removed:
                        logger.info('Pruned %d stale order books', removed)

        logger.info(
            'MultiStrategyEngine stopped (events=%d, slots=%d)',
            self._global_event_count,
            len(self.slots),
        )

    async def stop(self) -> None:
        """Stop the engine and all slots."""
        self.running = False
        try:
            await self.data_source.stop()
        except Exception:
            logger.debug('data_source.stop() error (ignored)', exc_info=True)

        for slot in self.slots.values():
            if slot.state_store:
                try:
                    slot.state_store.save_all(slot.trader.position_manager, slot.perf)
                except Exception:
                    pass
            if slot.alerter:
                try:
                    await slot.alerter.on_engine_stop('stopped')
                except Exception:
                    pass

    def request_stop(self) -> None:
        self.running = False

    # ── Snapshot for monitoring ────────────────────────────────────────

    def get_slot_snapshot(self, slot: EngineSlot) -> dict[str, Any]:
        """Build a monitoring snapshot for one slot (compatible with ControlServer get_state)."""
        from coinjure.engine.control import _ticker_display_name

        trader = slot.trader
        md = self.market_data
        strategy = slot.strategy

        state: dict[str, Any] = {
            'ok': True,
            'slot_id': slot.slot_id,
            'paused': slot.paused,
            'data_paused': False,
        }
        state['runtime'] = (
            str(datetime.now() - self._start_time).split('.')[0]
            if self._start_time
            else '0:00:00'
        )
        state['strategy_name'] = strategy.name or ''

        # Stats
        orders_list = list(trader.orders)
        decision_stats = strategy.get_decision_stats()
        state['stats'] = {
            'event_count': slot.event_count,
            'order_books': len(md.order_books),
            'news_buffered': len(slot.news),
            'decision_stats': decision_stats,
            'decisions': int(decision_stats.get('decisions', 0)),
            'executed': int(decision_stats.get('executed', 0)),
            'orders_total': len(orders_list),
            'orders_filled': sum(1 for o in orders_list if o.status.value == 'filled'),
        }

        # Portfolio
        try:
            pm = trader.position_manager
            pv = pm.get_portfolio_value(md)
            total = float(sum(pv.values(), Decimal('0')))
            realized = float(pm.get_total_realized_pnl())
            unrealized = float(pm.get_total_unrealized_pnl(md))
            state['portfolio'] = {
                'total': total,
                'cash_positions': [
                    {'symbol': p.ticker.symbol, 'qty': float(p.quantity)}
                    for p in pm.get_cash_positions()
                ],
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
            }
        except Exception:
            state['portfolio'] = {
                'total': 0,
                'cash_positions': [],
                'realized_pnl': 0,
                'unrealized_pnl': 0,
            }

        # Decisions
        try:
            decisions = list(strategy.get_decisions())
            state['decisions'] = [
                {
                    'timestamp': d.timestamp,
                    'action': d.action,
                    'confidence': float(getattr(d, 'confidence', 0.0) or 0.0),
                    'signal_values': {
                        str(k): float(v)
                        for k, v in (getattr(d, 'signal_values', {}) or {}).items()
                    },
                    'ticker_name': (d.ticker_name or '')[:30],
                    'reasoning': (getattr(d, 'reasoning', '') or '')[:60],
                    'executed': bool(d.executed),
                }
                for d in decisions[-40:]
            ]
        except Exception:
            state['decisions'] = []

        # Positions
        try:
            pm = trader.position_manager
            pos_list = []
            for p in pm.get_non_cash_positions():
                if p.quantity <= 0:
                    continue
                bid = md.get_best_bid(p.ticker)
                cur = float(bid.price) if bid else 0.0
                pnl = (
                    (cur - float(p.average_cost)) * float(p.quantity)
                    if cur > 0
                    else 0.0
                )
                pos_list.append(
                    {
                        'name': _ticker_display_name(p.ticker),
                        'side': (getattr(p.ticker, 'side', '') or '').upper(),
                        'qty': str(p.quantity),
                        'avg_cost': str(p.average_cost),
                        'bid': f'{cur:.4f}',
                        'pnl': f'{pnl:+.2f}',
                    }
                )
            state['positions'] = pos_list
        except Exception:
            state['positions'] = []

        # Orders — include index so monitor can show recency
        try:
            recent = orders_list[-8:]
            start_idx = len(orders_list) - len(recent)
            state['orders'] = [
                {
                    'idx': start_idx + i,
                    'side': o.side.value,
                    'name': _ticker_display_name(o.ticker),
                    'yn': (getattr(o.ticker, 'side', '') or '').upper(),
                    'limit_price': str(o.limit_price),
                    'status': o.status.value,
                }
                for i, o in enumerate(orders_list[-8:])
            ]
        except Exception:
            state['orders'] = []

        state['activity_log'] = list(slot.activity_log)
        state['news'] = list(slot.news)

        # Order books (shared)
        try:
            from coinjure.ticker import CashTicker

            books = []
            for ticker, ob in list(md.order_books.items()):
                if isinstance(ticker, CashTicker):
                    continue
                bid_lvl, ask_lvl = ob.best_bid, ob.best_ask
                if not (bid_lvl and ask_lvl and bid_lvl.price > 0):
                    continue
                mid = float(bid_lvl.price + ask_lvl.price) / 2
                spread = float(ask_lvl.price - bid_lvl.price)
                books.append(
                    {
                        'name': _ticker_display_name(ticker),
                        'yn': (getattr(ticker, 'side', '') or '').upper(),
                        'bid': f'{float(bid_lvl.price):.4f}',
                        'ask': f'{float(ask_lvl.price):.4f}',
                        'spread': f'{spread:.4f}',
                        'mid': f'{mid * 100:.0f}',
                        '_sort': abs(mid - 0.5),
                    }
                )
            books.sort(key=lambda x: cast(float, x.pop('_sort')))
            state['order_books'] = books[:40]
        except Exception:
            state['order_books'] = []

        return state


# ── MultiControlServer ─────────────────────────────────────────────────


SOCKET_DIR = Path.home() / '.coinjure'


class MultiControlServer:
    """Unix socket control server for MultiStrategyEngine.

    Protocol is the same JSON-over-newline as ControlServer, but commands
    accept an optional ``strategy_id`` to target a specific slot.  Commands
    without ``strategy_id`` operate on all slots or return aggregate info.
    """

    def __init__(
        self, engine: MultiStrategyEngine, socket_path: Path | None = None
    ) -> None:
        self.engine = engine
        self.socket_path = socket_path or (SOCKET_DIR / f'engine-{os.getpid()}.sock')
        self._start_time = datetime.now()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        logger.info('MultiControlServer ready on %s', self.socket_path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.socket_path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any] = {'ok': False, 'error': 'no request'}
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            request = json.loads(raw.decode())
            response = await self._dispatch(request)
        except (json.JSONDecodeError, asyncio.TimeoutError) as exc:
            response = {'ok': False, 'error': str(exc)}
        except Exception as exc:
            response = {'ok': False, 'error': str(exc)}
        finally:
            try:
                writer.write((json.dumps(response, default=str) + '\n').encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, req: dict) -> dict[str, Any]:
        cmd = req.get('cmd', '')
        strategy_id = req.get('strategy_id')

        if cmd == 'pause':
            return self._cmd_pause(strategy_id)
        if cmd == 'resume':
            return self._cmd_resume(strategy_id)
        if cmd == 'stop':
            if strategy_id:
                return self._cmd_stop_slot(strategy_id)
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self.engine.stop())
            )
            return {'ok': True, 'status': 'stopping'}
        if cmd == 'status':
            return self._cmd_status(strategy_id)
        if cmd == 'get_state':
            return self._cmd_get_state(strategy_id)
        if cmd == 'list_slots':
            return self._cmd_list_slots()
        return {'ok': False, 'error': f'Unknown command: {cmd!r}'}

    def _cmd_pause(self, strategy_id: str | None) -> dict:
        slots = self._resolve_slots(strategy_id)
        if not slots:
            return {'ok': False, 'error': f'No slot: {strategy_id}'}
        for slot in slots:
            slot.paused = True
            slot.strategy.set_paused(True)
            slot.trader.set_read_only(True)
        return {'ok': True, 'status': 'paused', 'count': len(slots)}

    def _cmd_resume(self, strategy_id: str | None) -> dict:
        slots = self._resolve_slots(strategy_id)
        if not slots:
            return {'ok': False, 'error': f'No slot: {strategy_id}'}
        for slot in slots:
            slot.paused = False
            slot.strategy.set_paused(False)
            slot.trader.set_read_only(False)
        return {'ok': True, 'status': 'running', 'count': len(slots)}

    def _cmd_stop_slot(self, strategy_id: str) -> dict:
        slot = self.engine.get_slot(strategy_id)
        if not slot:
            return {'ok': False, 'error': f'No slot: {strategy_id}'}
        slot.paused = True
        slot.strategy.set_paused(True)
        slot.trader.set_read_only(True)
        # Remove from active slots
        self.engine.slots.pop(strategy_id, None)
        if not self.engine.slots:
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self.engine.stop())
            )
        return {'ok': True, 'status': 'slot_stopped', 'strategy_id': strategy_id}

    def _cmd_status(self, strategy_id: str | None) -> dict:
        if strategy_id:
            slot = self.engine.get_slot(strategy_id)
            if not slot:
                return {'ok': False, 'error': f'No slot: {strategy_id}'}
            return self._slot_status(slot)

        # Aggregate summary
        runtime = (
            str(datetime.now() - self._start_time).split('.')[0]
            if self._start_time
            else '0:00:00'
        )
        total_events = sum(s.event_count for s in self.engine.slots.values())
        total_orders = sum(len(s.trader.orders) for s in self.engine.slots.values())
        return {
            'ok': True,
            'mode': 'multi',
            'runtime': runtime,
            'slots': len(self.engine.slots),
            'active_slots': len(self.engine.active_slots),
            'total_events': total_events,
            'total_orders': total_orders,
            'slot_ids': list(self.engine.slots.keys()),
        }

    def _cmd_get_state(self, strategy_id: str | None) -> dict:
        if strategy_id:
            slot = self.engine.get_slot(strategy_id)
            if not slot:
                return {'ok': False, 'error': f'No slot: {strategy_id}'}
            return self.engine.get_slot_snapshot(slot)

        # Aggregate all slot snapshots into a single monitor-compatible state.
        # The SocketTradingMonitorApp expects top-level keys like decisions,
        # positions, orders, portfolio, activity_log, etc.
        snapshots = [
            self.engine.get_slot_snapshot(s) for s in self.engine.slots.values()
        ]
        if not snapshots:
            return {
                'ok': True,
                'paused': False,
                'data_paused': False,
                'runtime': '0:00:00',
                'strategy_name': '',
                'stats': {},
                'portfolio': {},
                'decisions': [],
                'positions': [],
                'orders': [],
                'order_books': [],
                'activity_log': [],
                'news': [],
            }

        runtime = (
            str(datetime.now() - self._start_time).split('.')[0]
            if self._start_time
            else '0:00:00'
        )

        # Merge portfolio
        total = sum(s.get('portfolio', {}).get('total', 0.0) for s in snapshots)
        realized = sum(
            s.get('portfolio', {}).get('realized_pnl', 0.0) for s in snapshots
        )
        unrealized = sum(
            s.get('portfolio', {}).get('unrealized_pnl', 0.0) for s in snapshots
        )
        cash_positions: list[dict] = []
        for s in snapshots:
            cash_positions.extend(s.get('portfolio', {}).get('cash_positions', []))

        # Merge decisions — tag each with its slot's strategy name
        merged_decisions: list[dict] = []
        for s in snapshots:
            sname = s.get('strategy_name', '')
            for d in s.get('decisions', []):
                d2 = dict(d)
                d2.setdefault('strategy_name', sname)
                merged_decisions.append(d2)
        merged_decisions.sort(key=lambda d: d.get('timestamp', ''))

        # Merge positions, orders, activity, news
        merged_positions: list[dict] = []
        merged_orders: list[dict] = []
        merged_activity: list[tuple] = []
        merged_news: list[dict] = []
        for s in snapshots:
            merged_positions.extend(s.get('positions', []))
            merged_orders.extend(s.get('orders', []))
            merged_activity.extend(s.get('activity_log', []))
            merged_news.extend(s.get('news', []))

        # Aggregate stats
        total_events = sum(s.get('stats', {}).get('event_count', 0) for s in snapshots)
        total_decisions = sum(s.get('stats', {}).get('decisions', 0) for s in snapshots)
        total_executed = sum(s.get('stats', {}).get('executed', 0) for s in snapshots)
        total_orders = sum(s.get('stats', {}).get('orders_total', 0) for s in snapshots)
        total_filled = sum(
            s.get('stats', {}).get('orders_filled', 0) for s in snapshots
        )

        # Use order_books from the first snapshot (shared DataManager)
        order_books = snapshots[0].get('order_books', []) if snapshots else []

        any_paused = any(s.get('paused', False) for s in snapshots)
        strategy_names = [
            s.get('strategy_name', '') for s in snapshots if s.get('strategy_name')
        ]

        return {
            'ok': True,
            'mode': 'multi',
            'paused': any_paused,
            'data_paused': False,
            'runtime': runtime,
            'strategy_name': ', '.join(strategy_names),
            'stats': {
                'event_count': total_events,
                'order_books': len(order_books),
                'news_buffered': len(merged_news),
                'decision_stats': {
                    'decisions': total_decisions,
                    'executed': total_executed,
                },
                'decisions': total_decisions,
                'executed': total_executed,
                'orders_total': total_orders,
                'orders_filled': total_filled,
            },
            'portfolio': {
                'total': total,
                'cash_positions': cash_positions,
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
            },
            'decisions': merged_decisions[-40:],
            'positions': merged_positions,
            'orders': merged_orders[-16:],
            'order_books': order_books,
            'activity_log': merged_activity[-100:],
            'news': merged_news[-50:],
        }

    def _cmd_list_slots(self) -> dict:
        return {
            'ok': True,
            'slots': [
                {
                    'slot_id': s.slot_id,
                    'strategy_name': s.strategy.name or '',
                    'paused': s.paused,
                    'degraded': s.degraded_read_only,
                    'event_count': s.event_count,
                    'orders': len(s.trader.orders),
                }
                for s in self.engine.slots.values()
            ],
        }

    def _slot_status(self, slot: EngineSlot) -> dict:
        md = self.engine.market_data
        decision_stats = slot.strategy.get_decision_stats()
        try:
            pm = slot.trader.position_manager
            pv = pm.get_portfolio_value(md)
            total = float(sum(pv.values(), Decimal('0')))
            realized = float(pm.get_total_realized_pnl())
            unrealized = float(pm.get_total_unrealized_pnl(md))
            portfolio = {
                'total': total,
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
            }
        except Exception:
            portfolio = {'total': 0, 'realized_pnl': 0, 'unrealized_pnl': 0}

        return {
            'ok': True,
            'slot_id': slot.slot_id,
            'strategy_name': slot.strategy.name or '',
            'paused': slot.paused,
            'degraded': slot.degraded_read_only,
            'event_count': slot.event_count,
            'decisions': int(decision_stats.get('decisions', 0)),
            'executed': int(decision_stats.get('executed', 0)),
            'orders': len(slot.trader.orders),
            'portfolio': portfolio,
        }

    def _resolve_slots(self, strategy_id: str | None) -> list[EngineSlot]:
        if strategy_id:
            slot = self.engine.get_slot(strategy_id)
            return [slot] if slot else []
        return list(self.engine.slots.values())
