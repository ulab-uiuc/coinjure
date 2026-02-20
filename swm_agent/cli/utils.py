"""Utility functions for CLI integration with trading engine."""

import logging
import threading
import time

from rich.live import Live

from swm_agent.cli.monitor import TradingMonitor
from swm_agent.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


class MonitoredTradingEngine:
    """Wrapper for TradingEngine that runs with live monitoring in watch mode."""

    def __init__(
        self,
        engine: TradingEngine,
        refresh_rate: float = 2.0,
        enabled: bool = True,
        exchange_name: str = '',
    ) -> None:
        self.engine = engine
        self.refresh_rate = refresh_rate
        self.enabled = enabled
        self.exchange_name = exchange_name
        self.monitor: TradingMonitor | None = None
        self._monitor_thread: threading.Thread | None = None

    def _sync_data(self) -> None:
        """Sync engine data into monitor for display.

        Copies all mutable collections so the monitor thread never
        iterates over objects the engine thread is mutating.
        """
        if not self.monitor:
            return

        try:
            # Sync LLM decisions from strategy
            decisions = getattr(self.engine.strategy, 'decisions', None)
            if decisions is not None:
                self.monitor.llm_decisions = list(decisions)  # type: ignore[attr-defined]

            # Sync running counters (not affected by deque eviction)
            self.monitor.total_executed = getattr(self.engine.strategy, 'total_executed', 0)
            self.monitor.total_decisions = getattr(self.engine.strategy, 'total_decisions', 0)
            self.monitor.total_buy_yes = getattr(self.engine.strategy, 'total_buy_yes', 0)
            self.monitor.total_buy_no = getattr(self.engine.strategy, 'total_buy_no', 0)
            self.monitor.total_holds = getattr(self.engine.strategy, 'total_holds', 0)
            self.monitor.total_closes = getattr(self.engine.strategy, 'total_closes', 0)

            # Sync activity log from engine
            self.monitor.activity_log = list(self.engine._activity_log)  # type: ignore[attr-defined]

            # Sync news headlines from engine
            self.monitor.news_headlines = list(self.engine._news)  # type: ignore[attr-defined]

            # Sync event count from engine
            self.monitor.event_count = self.engine._event_count  # type: ignore[attr-defined]

            # Sync news buffer count from strategy
            self.monitor.news_buffer_count = getattr(self.engine.strategy, 'news_buffer_count', 0)  # type: ignore[attr-defined]

            # Sync performance stats from engine's performance analyzer
            perf = getattr(self.engine, '_perf', None)
            if perf is not None:
                stats = perf.get_stats()
                self.monitor.perf_stats = stats  # type: ignore[attr-defined]

            # Sync order book count
            try:
                self.monitor.ob_count = len(self.engine.market_data.order_books)  # type: ignore[attr-defined]
            except (RuntimeError, AttributeError):
                pass
        except (RuntimeError, Exception) as e:
            # RuntimeError: dict/deque changed size during iteration
            # This is harmless — we'll get the data on the next refresh
            logger.debug('sync_data transient error: %s', e)

    def _run_monitor_thread(self) -> None:
        """Run monitor in a separate thread with data sync."""
        if not self.monitor:
            return
        try:
            with Live(
                self.monitor.create_layout(),
                console=self.monitor.console,
                refresh_per_second=4,
                screen=True,
            ) as live:
                while True:
                    try:
                        self._sync_data()
                        live.update(self.monitor.create_layout())
                    except (RuntimeError, KeyError, AttributeError) as e:
                        # Transient thread-safety errors from iterating shared
                        # dicts/lists while the engine mutates them.
                        # Log and retry on next refresh cycle.
                        logger.debug('Monitor render error (retrying): %s', e)
                    except Exception as e:
                        # Unexpected error — log but keep the thread alive
                        logger.warning('Monitor error: %s', e, exc_info=True)

                    time.sleep(self.refresh_rate)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.error('Monitor thread fatal error: %s', e, exc_info=True)

    async def start(self) -> None:
        """Start the trading engine with monitoring."""
        if self.enabled:
            import asyncio

            self.monitor = TradingMonitor(
                trader=self.engine.trader,
                position_manager=self.engine.trader.position_manager,
                exchange_name=self.exchange_name,
            )

            self._monitor_thread = threading.Thread(
                target=self._run_monitor_thread, daemon=True
            )
            self._monitor_thread.start()

            await asyncio.sleep(0.5)

        await self.engine.start()

    async def stop(self) -> None:
        """Stop the trading engine and monitoring."""
        await self.engine.stop()

    def display_snapshot(self) -> None:
        """Display a snapshot of current state (for non-watch mode)."""
        if self.monitor is None:
            self.monitor = TradingMonitor(
                trader=self.engine.trader,
                position_manager=self.engine.trader.position_manager,
                exchange_name=self.exchange_name,
            )
        self.monitor.display_snapshot()


def add_monitoring_to_engine(
    engine: TradingEngine,
    watch: bool = False,
    refresh_rate: float = 2.0,
    exchange_name: str = '',
) -> MonitoredTradingEngine:
    """Add monitoring capabilities to an existing trading engine.

    Args:
        engine: The trading engine to monitor
        watch: Enable live watch mode
        refresh_rate: Refresh rate for watch mode in seconds
        exchange_name: Exchange name to show in monitor header

    Returns:
        A MonitoredTradingEngine that wraps the original engine
    """
    return MonitoredTradingEngine(
        engine=engine,
        refresh_rate=refresh_rate,
        enabled=watch,
        exchange_name=exchange_name,
    )
