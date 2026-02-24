"""Utility functions for CLI integration with trading engine."""

import logging

from swm_agent.cli.monitor import TradingMonitor
from swm_agent.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


class MonitoredTradingEngine:
    """Wrapper for TradingEngine with an optional Textual live monitor.

    Architecture when monitor is enabled:
    - Main thread runs Textual (via ``app.run_async()``) — required so that
      Textual's LinuxDriver can register signal handlers (SIGTSTP/SIGCONT).
    - The trading engine runs *inside* Textual's asyncio event loop as a
      Textual Worker (``app.run_worker(engine.start())``), so no second thread
      is needed and there are no signal-handler conflicts.
    """

    def __init__(
        self,
        engine: TradingEngine,
        refresh_rate: float = 2.0,  # kept for API compatibility
        enabled: bool = True,
        exchange_name: str = '',
    ) -> None:
        self.engine = engine
        self.refresh_rate = refresh_rate
        self.enabled = enabled
        self.exchange_name = exchange_name
        self.monitor: TradingMonitor | None = None  # used by display_snapshot()

    async def start(self) -> None:
        """Start the engine, optionally with the Textual live monitor.

        When *enabled*, Textual runs in the current event loop (main thread)
        and the engine is launched as a Textual worker — no daemon threads,
        no signal-handler errors.

        When *disabled*, the engine starts directly (non-interactive).
        """
        if self.enabled:
            from swm_agent.cli.textual_monitor import TradingMonitorApp

            app = TradingMonitorApp(
                engine=self.engine, exchange_name=self.exchange_name
            )
            # run_async() runs Textual inside the *current* asyncio event loop,
            # which lives on the main thread — so signal.signal() works fine.
            await app.run_async()
        else:
            await self.engine.start()

    async def stop(self) -> None:
        """Stop the trading engine."""
        await self.engine.stop()

    def display_snapshot(self) -> None:
        """One-shot Rich snapshot (used when monitor is disabled)."""
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
    """Wrap a TradingEngine with optional Textual live monitoring.

    Args:
        engine: The trading engine to monitor.
        watch: Enable live Textual watch mode.
        refresh_rate: Refresh interval hint (seconds).
        exchange_name: Exchange name displayed in the monitor header.

    Returns:
        A MonitoredTradingEngine wrapping the original engine.
    """
    return MonitoredTradingEngine(
        engine=engine,
        refresh_rate=refresh_rate,
        enabled=watch,
        exchange_name=exchange_name,
    )
