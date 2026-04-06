"""Trading engine with snapshot support for live monitoring."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from coinjure.data.source import DataSource
from coinjure.engine.performance import PerformanceAnalyzer
from coinjure.events import NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.trading.risk import StandardRiskManager
from coinjure.trading.trader import Trader
from coinjure.trading.types import OrderStatus

if TYPE_CHECKING:
    from coinjure.engine.trader.alerter import Alerter
    from coinjure.storage.state_store import StateStore

logger = logging.getLogger(__name__)

# Maximum consecutive ``None`` events before the engine warns (continuous mode).
_MAX_CONSECUTIVE_NONE = 120  # ~2 min at 1 s timeout per poll


class TradingEngine:
    def __init__(
        self,
        data_source: DataSource,
        strategy: Strategy,
        trader: Trader,
        *,
        initial_capital: Decimal = Decimal('10000'),
        continuous: bool = False,
        state_store: StateStore | None = None,
        alerter: Alerter | None = None,
        drawdown_alert_pct: Decimal | None = None,
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

        # --- persistence & alerting ---
        self._state_store = state_store
        self._alerter = alerter
        self._drawdown_alert_pct = drawdown_alert_pct
        self._drawdown_alerted: bool = False  # avoid repeated alerts
        self._SAVE_INTERVAL = 100  # save every N events
        self._save_event_counter: int = 0
        self._consecutive_processing_errors: int = 0
        self._MAX_CONSECUTIVE_ERRORS: int = 5
        self._degraded_read_only: bool = False

        # --- monitoring helpers ---
        self._start_time: datetime | None = None
        self._event_count: int = 0
        self._last_orders_idx: int = 0
        self._last_trade_count: int = 0
        self._order_times: list[str] = []
        self._perf = PerformanceAnalyzer(initial_capital=initial_capital)
        self._news: deque[dict[str, str]] = deque(maxlen=300)
        self._activity_log: deque[tuple[str, str]] = deque(maxlen=100)
        self._last_decisions_count: int = 0

        # [H3] Guard: prevent calling data_source.start() more than once.
        self._ds_started: bool = False

        # [M4] Periodic stale order book pruning interval (by event count).
        self._last_prune_event: int = 0
        self._PRUNE_INTERVAL = 500

        # [P1] Deferred news events: collected during market-event draining.
        self._deferred_news: deque[NewsEvent] = deque(maxlen=500)

        # [D1] Data-flow pause flag: when True the event loop sleeps instead
        # of calling data_source.get_next_event().  Set by ControlServer.
        self._data_paused: bool = False

        # [LLM] Periodic LLM portfolio review counter
        self._llm_review_event_counter: int = 0
        self._LLM_REVIEW_INTERVAL = 500

    def _drain_backtest_timestamp_batch(self, event: object) -> list[object]:
        """Return all queued events that should be processed as a batch.

        In continuous (live) mode, drain all pending events from the queue
        so that order books are fully updated before the strategy runs.
        In backtest mode, drain events that share a timestamp.
        """
        if self._continuous:
            # Drain all pending events so order books are complete
            batch = [event]
            drain_queue = getattr(self.data_source, 'drain_pending_events', None)
            if callable(drain_queue):
                batch.extend(drain_queue())
            return batch

        drain = getattr(self.data_source, 'drain_same_timestamp_events', None)
        if not callable(drain):
            return [event]

        try:
            drained = drain(event)
        except Exception:
            logger.debug(
                'Failed to drain same-timestamp backtest events',
                exc_info=True,
            )
            return [event]

        if not drained:
            return [event]
        return [event, *drained]

    def _apply_market_event(self, event: object) -> None:
        if isinstance(event, OrderBookEvent):
            self.market_data.process_orderbook_event(event)
        elif isinstance(event, PriceChangeEvent):
            self.market_data.process_price_change_event(event)
            logger.debug('Price %s → %s', event.ticker.symbol, event.price)
        # Try filling resting orders after each market update
        if hasattr(self.trader, 'try_fill_resting_orders'):
            self.trader.try_fill_resting_orders()

    def _log_event_milestone(self) -> None:
        if self._event_count in (1, 10, 50, 100) or self._event_count % 500 == 0:
            now_str = datetime.now().strftime('%H:%M:%S')
            ob_count = len(self.market_data.order_books)
            self._activity_log.append(
                (
                    now_str,
                    f'Collected {self._event_count} market data points ({ob_count} order books)',
                )
            )

    async def _process_one_event(  # noqa: C901
        self,
        event: object,
        *,
        market_data_already_applied: bool = False,
    ) -> None:
        self._event_count += 1
        self._log_event_milestone()

        try:
            now_str = datetime.now().strftime('%H:%M:%S')

            if not market_data_already_applied:
                self._apply_market_event(event)

            if isinstance(event, NewsEvent):
                headline = event.title or event.news[:100]
                source = getattr(event, 'source', '') or ''
                url = getattr(event, 'url', '') or ''
                self.trader.record_news(
                    timestamp=now_str,
                    title=headline,
                    source=source,
                    url=url,
                )
                self._news.append(
                    {
                        'timestamp': now_str,
                        'title': headline,
                        'source': source,
                        'url': url,
                    }
                )
                _crawl_sources = {'polymarket', 'kalshi'}
                _label = 'Crawl' if source.lower() in _crawl_sources else 'News'
                self._activity_log.append(
                    (now_str, f'{_label} [{source[:15]}] "{headline[:55]}"')
                )
                logger.info('NewsEvent: %s', headline)

            prev_orders = len(self.trader.orders)
            self.strategy.bind_context(event, self.trader)
            await self.strategy.process_event(event, self.trader)

            decisions = self.strategy.get_decisions()
            stats = self.strategy.get_decision_stats()
            total_d = int(stats.get('decisions', len(decisions)))
            if total_d > self._last_decisions_count:
                new_count = total_d - self._last_decisions_count
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
                    self._activity_log.append(
                        (now_str, f'{d.action} {signal_str} [{exec_mark}] "{name}"')
                    )
                self._last_decisions_count = total_d

            await self._sync_trades()
            new_orders = self.trader.orders[prev_orders:]
            for order in new_orders:
                status = order.status.value.upper()
                side = order.side.value.upper()
                ticker_name = (
                    getattr(order.ticker, 'name', '') or order.ticker.symbol[:25]
                )
                self._activity_log.append(
                    (
                        now_str,
                        f'{side} {order.filled_quantity} @ ${order.average_price:.4f} → {status} "{ticker_name[:25]}"',
                    )
                )
                if hasattr(order.ticker, 'token_id'):
                    if order.side.value.upper() == 'BUY':
                        watch = getattr(self.data_source, 'watch_token', None)
                        if watch:
                            watch(order.ticker.token_id)
                            complement = self.market_data.find_complement(order.ticker)
                            if complement and hasattr(complement, 'token_id'):
                                watch(complement.token_id)
                    elif order.side.value.upper() == 'SELL':
                        pos = self.trader.position_manager.get_position(order.ticker)
                        if pos is None or pos.quantity <= 0:
                            unwatch = getattr(self.data_source, 'unwatch_token', None)
                            if unwatch:
                                unwatch(order.ticker.token_id)
        except Exception as exc:
            now_str = datetime.now().strftime('%H:%M:%S')
            self._activity_log.append(
                (now_str, f'Error processing event #{self._event_count}')
            )
            logger.exception('Error processing event #%d', self._event_count)
            self._consecutive_processing_errors += 1
            if self._alerter:
                try:
                    await self._alerter.on_error(exc)
                except Exception:
                    pass
            await self._auto_degrade_if_needed()
        else:
            self._consecutive_processing_errors = 0

        self._save_event_counter += 1
        if self._save_event_counter >= self._SAVE_INTERVAL:
            self._save_event_counter = 0
            if self._state_store:
                try:
                    self._state_store.save_all(self.trader.position_manager, self._perf)
                except Exception:
                    logger.debug(
                        'periodic state_store.save_all() failed',
                        exc_info=True,
                    )
            await self._check_drawdown_alert()
            await self._check_portfolio_health()

        self._llm_review_event_counter += 1
        if self._llm_review_event_counter >= self._LLM_REVIEW_INTERVAL:
            self._llm_review_event_counter = 0
            await self._check_llm_portfolio_review()

        if self._event_count - self._last_prune_event >= self._PRUNE_INTERVAL:
            self._last_prune_event = self._event_count
            known = getattr(self.data_source, '_known_tickers', None)
            if known is None:
                for src in getattr(self.data_source, 'sources', []):
                    known = getattr(src, '_known_tickers', None)
                    if known is not None:
                        break
            if known is not None:
                removed = self.market_data.prune_stale_tickers(set(known.keys()))
                if removed:
                    logger.info(
                        'Pruned %d stale order books from DataManager',
                        removed,
                    )

    # ------------------------------------------------------------------ #
    # Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:  # noqa: C901
        self._start_time = datetime.now()
        self.running = True

        # [H3] Start the data source exactly once.
        if not self._ds_started:
            self._ds_started = True
            await self.data_source.start()

        # Register strategy's priority tokens with the data source
        watch = getattr(self.data_source, 'watch_token', None)
        register = getattr(self.data_source, 'register_token_ticker', None)
        if watch:
            # Pre-register tickers with correct market_id/side if strategy provides them
            if register:
                for attr in ('_yes_tickers', '_no_tickers'):
                    tickers = getattr(self.strategy, attr, {})
                    for tid, ticker in tickers.items():
                        register(tid, ticker)
            for token_id in self.strategy.watch_tokens():
                watch(token_id)

        if self._alerter:
            try:
                await self._alerter.on_engine_start()
            except Exception:
                logger.debug('alerter.on_engine_start() failed', exc_info=True)

        try:
            await self.strategy.on_start()
        except Exception:
            logger.debug('strategy.on_start() failed', exc_info=True)

        logger.info('TradingEngine started (continuous=%s)', self._continuous)

        # [M1] Track consecutive None events so we can warn on prolonged
        # silence instead of silently spinning.
        consecutive_none = 0

        # [C1] Exponential backoff state for data-fetch errors.
        _fetch_backoff: float = 1.0
        _FETCH_BACKOFF_BASE: float = 1.0
        _FETCH_BACKOFF_CAP: float = 30.0
        _consecutive_fetch_failures: int = 0
        _MAX_FETCH_FAILURES: int = 10

        while self.running:
            # [D1] When data flow is paused, sleep instead of polling the source.
            if self._data_paused:
                await asyncio.sleep(0.5)
                continue

            try:
                event = await self.data_source.get_next_event()
            except Exception:
                _consecutive_fetch_failures += 1
                logger.exception(
                    'Error fetching next event (attempt %d, backoff %.1fs)',
                    _consecutive_fetch_failures,
                    _fetch_backoff,
                )
                # [C1] Exponential back off on repeated fetch errors.
                await asyncio.sleep(_fetch_backoff)
                _fetch_backoff = min(_fetch_backoff * 2, _FETCH_BACKOFF_CAP)

                if _consecutive_fetch_failures >= _MAX_FETCH_FAILURES:
                    msg = (
                        f'{_consecutive_fetch_failures} consecutive data-fetch '
                        'failures — freezing positions'
                    )
                    logger.error(msg)
                    self.trader.set_read_only(True)
                    self.strategy.set_paused(True)
                    self._degraded_read_only = True
                    if self._alerter:
                        try:
                            await self._alerter.on_risk_limit_hit(
                                'data_fetch_failure_storm'
                            )
                        except Exception:
                            pass
                continue

            # Reset backoff on successful fetch.
            if _consecutive_fetch_failures > 0:
                logger.info(
                    'Data fetch recovered after %d failures',
                    _consecutive_fetch_failures,
                )
            _fetch_backoff = _FETCH_BACKOFF_BASE
            _consecutive_fetch_failures = 0

            if event is None:
                # [P1] Process deferred news when the queue is idle.
                if self._deferred_news:
                    event = self._deferred_news.popleft()
                elif self._continuous:
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
                else:
                    self.running = False
                    logger.info('Data source exhausted — engine stopping')
                    break

            # Got a real event — reset the silence counter.
            consecutive_none = 0

            # [P1] Before processing a NewsEvent, drain all pending market
            # data events so order books are up-to-date.  This prevents the
            # scenario where order books stay empty while the engine
            # processes a burst of news events one by one — especially
            # important when using the Market Data Hub, where order book
            # events arrive on a 5-second refresh cycle while RSS news
            # streams continuously.
            _news_drain_extra: list[object] = []
            if self._continuous and isinstance(event, NewsEvent):
                drained = 0
                while True:
                    try:
                        peek = await asyncio.wait_for(
                            self.data_source.get_next_event(),
                            timeout=0.05,
                        )
                    except (asyncio.TimeoutError, Exception):
                        break
                    if peek is None:
                        break
                    self._event_count += 1
                    if isinstance(peek, OrderBookEvent):
                        self.market_data.process_orderbook_event(peek)
                        drained += 1
                    elif isinstance(peek, PriceChangeEvent):
                        self.market_data.process_price_change_event(peek)
                        _news_drain_extra.append(peek)
                        drained += 1
                    elif isinstance(peek, NewsEvent):
                        # Put non-market events into a buffer to process later
                        self._deferred_news.append(peek)
                if drained > 0:
                    logger.debug('Drained %d market events before NewsEvent', drained)

            batch = self._drain_backtest_timestamp_batch(event)
            if _news_drain_extra:
                batch.extend(_news_drain_extra)
            batch_has_prefetched_market_updates = len(batch) > 1
            if batch_has_prefetched_market_updates:
                for batch_event in batch:
                    self._apply_market_event(batch_event)

            for i, batch_event in enumerate(batch):
                await self._process_one_event(
                    batch_event,
                    market_data_already_applied=batch_has_prefetched_market_updates,
                )
                # [Y1] Yield to the event loop periodically during large batches
                # so the ControlServer can handle incoming commands (status, pause,
                # stop) without starving.  Without this, drain_pending_events()
                # can create batches of 2000+ events that monopolise the loop.
                if i & 63 == 63:  # every 64 events
                    await asyncio.sleep(0)

            # [Y2] Yield once at the end of every main-loop iteration to
            # guarantee the event loop processes I/O (control socket, hub
            # reader) even when the data queue is always non-empty.
            await asyncio.sleep(0)

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

        if self._state_store:
            try:
                self._state_store.save_all(self.trader.position_manager, self._perf)
            except Exception:
                logger.debug('state_store.save_all() on stop failed', exc_info=True)

        if self._alerter:
            try:
                await self._alerter.on_engine_stop('stopped')
            except Exception:
                pass

    def request_stop(self) -> None:
        """Non-async flag-flip — safe to call from any context."""
        self.running = False

    # ------------------------------------------------------------------ #
    # Trade tracking                                                      #
    # ------------------------------------------------------------------ #

    async def _sync_trades(self) -> None:
        """Feed newly-appeared orders/trades into the performance analyser.

        Also persists new trades/orders to the state store and fires
        alerter.on_trade() for filled orders.
        Handles both new orders and resting orders that fill later.
        """
        orders = self.trader.orders
        now_str = datetime.now().strftime('%H:%M:%S')

        # Process new orders
        while self._last_orders_idx < len(orders):
            order = orders[self._last_orders_idx]
            self._order_times.append(now_str)
            if self._state_store:
                try:
                    self._state_store.append_order(order)
                except Exception:
                    logger.debug('state_store.append_order() failed', exc_info=True)
            if self._alerter and order.status not in (
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
            ):
                try:
                    await self._alerter.on_order_placed(order)
                except Exception:
                    logger.debug('alerter.on_order_placed() failed', exc_info=True)
            self._last_orders_idx += 1

        # Scan all orders for new trades (handles resting order fills)
        total_trades = sum(len(o.trades) for o in orders)
        if total_trades > self._last_trade_count:
            # Find and record new trades
            seen = 0
            for order in orders:
                for trade in order.trades:
                    seen += 1
                    if seen > self._last_trade_count:
                        self._perf.add_trade(trade)
                        if self._state_store:
                            try:
                                self._state_store.append_trade(trade)
                            except Exception:
                                logger.debug(
                                    'state_store.append_trade() failed', exc_info=True
                                )
                        if self._alerter and order.status in (
                            OrderStatus.FILLED,
                            OrderStatus.PARTIALLY_FILLED,
                        ):
                            try:
                                await self._alerter.on_trade(trade)
                            except Exception:
                                logger.debug('alerter.on_trade() failed', exc_info=True)
            self._last_trade_count = total_trades

    async def _check_drawdown_alert(self) -> None:
        """Fire a drawdown alert if the current drawdown exceeds the threshold."""
        if not self._alerter or self._drawdown_alert_pct is None:
            return
        rm = self.trader.risk_manager
        if not isinstance(rm, StandardRiskManager):
            return
        try:
            current_dd = rm.get_current_drawdown()
            if current_dd >= self._drawdown_alert_pct and not self._drawdown_alerted:
                self._drawdown_alerted = True
                await self._alerter.on_drawdown_alert(
                    current_dd, self._drawdown_alert_pct
                )
            elif current_dd < self._drawdown_alert_pct:
                # Reset so we can alert again if drawdown recovers then worsens
                self._drawdown_alerted = False
        except Exception:
            logger.debug('_check_drawdown_alert() failed', exc_info=True)

    async def _check_portfolio_health(self) -> None:
        """Post-trade hard risk gate. Breaches force read-only degradation."""
        rm = self.trader.risk_manager
        if not isinstance(rm, StandardRiskManager):
            return
        try:
            ok, reason = rm.check_portfolio_health()
        except Exception:
            logger.debug('check_portfolio_health() failed', exc_info=True)
            return
        if ok or self._degraded_read_only:
            return

        now_str = datetime.now().strftime('%H:%M:%S')
        self._activity_log.append((now_str, f'Risk breach: {reason} -> READ-ONLY'))
        logger.error(
            'Risk breach detected (%s). Switching trader to read-only mode.', reason
        )
        self.trader.set_read_only(True)
        self.strategy.set_paused(True)
        self._degraded_read_only = True
        if self._alerter:
            try:
                await self._alerter.on_risk_limit_hit(reason)
            except Exception:
                pass

    async def _check_llm_portfolio_review(self) -> None:
        llm_portfolio_review = getattr(self.strategy, 'llm_portfolio_review', False)
        if not llm_portfolio_review:
            return

        llm_model = getattr(self.strategy, 'llm_model', None)
        kelly_fraction = getattr(self.strategy, 'kelly_fraction', None)
        max_trade_size = getattr(self.strategy, 'max_trade_size', None)
        if kelly_fraction is None or max_trade_size is None:
            return

        pm = self.trader.position_manager
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

        strategy_id = getattr(self.strategy, 'relation_id', None) or getattr(
            self.strategy, 'name', 'unknown'
        )
        trade_count = len(self.trader.orders)

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
            logger.debug('_check_llm_portfolio_review() call failed', exc_info=True)
            return

        if adjustment is None:
            return

        if adjustment.kelly_fraction is not None:
            old = self.strategy.kelly_fraction
            self.strategy.kelly_fraction = adjustment.kelly_fraction
            logger.info(
                'LLM portfolio review adjusted kelly_fraction: %s -> %s (%s)',
                old,
                adjustment.kelly_fraction,
                adjustment.reasoning,
            )

        if adjustment.max_trade_size is not None:
            old = self.strategy.max_trade_size
            self.strategy.max_trade_size = adjustment.max_trade_size
            logger.info(
                'LLM portfolio review adjusted max_trade_size: %s -> %s (%s)',
                old,
                adjustment.max_trade_size,
                adjustment.reasoning,
            )

    async def _auto_degrade_if_needed(self) -> None:
        """Pause strategy and force read-only mode on repeated processing errors."""
        if self._consecutive_processing_errors < self._MAX_CONSECUTIVE_ERRORS:
            return
        if self._degraded_read_only:
            return
        msg = (
            f'Consecutive processing errors={self._consecutive_processing_errors} '
            '-> auto-degrade READ-ONLY'
        )
        now_str = datetime.now().strftime('%H:%M:%S')
        self._activity_log.append((now_str, msg))
        logger.error(msg)
        self.trader.set_read_only(True)
        self.strategy.set_paused(True)
        self._degraded_read_only = True
        if self._alerter:
            try:
                await self._alerter.on_risk_limit_hit('auto_degrade_error_storm')
            except Exception:
                pass
