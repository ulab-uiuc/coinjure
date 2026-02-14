"""Utility functions for CLI integration with trading engine."""

import asyncio
import threading
from typing import Optional

from swm_agent.cli.monitor import TradingMonitor
from swm_agent.core.trading_engine import TradingEngine


class MonitoredTradingEngine:
    """Wrapper for TradingEngine that runs with live monitoring in watch mode."""

    def __init__(
        self, engine: TradingEngine, refresh_rate: float = 2.0, enabled: bool = True
    ) -> None:
        """Initialize monitored trading engine.

        Args:
            engine: The trading engine to monitor
            refresh_rate: Refresh rate for live monitoring in seconds
            enabled: Whether monitoring is enabled
        """
        self.engine = engine
        self.refresh_rate = refresh_rate
        self.enabled = enabled
        self.monitor: Optional[TradingMonitor] = None
        self._monitor_thread: Optional[threading.Thread] = None

    def _run_monitor_thread(self) -> None:
        """Run monitor in a separate thread."""
        if self.monitor:
            self.monitor.display_live(refresh_rate=self.refresh_rate)

    async def start(self) -> None:
        """Start the trading engine with monitoring."""
        if self.enabled:
            # Create monitor
            self.monitor = TradingMonitor(
                trader=self.engine.trader,
                position_manager=self.engine.trader.position_manager,
            )

            # Start monitor in separate thread
            self._monitor_thread = threading.Thread(
                target=self._run_monitor_thread, daemon=True
            )
            self._monitor_thread.start()

            # Small delay to let monitor initialize
            await asyncio.sleep(0.5)

        # Start trading engine
        await self.engine.start()

    def stop(self) -> None:
        """Stop the trading engine and monitoring."""
        self.engine.stop()

    def display_snapshot(self) -> None:
        """Display a snapshot of current state (for non-watch mode)."""
        if self.monitor is None:
            self.monitor = TradingMonitor(
                trader=self.engine.trader,
                position_manager=self.engine.trader.position_manager,
            )
        self.monitor.display_snapshot()


def add_monitoring_to_engine(
    engine: TradingEngine, watch: bool = False, refresh_rate: float = 2.0
) -> MonitoredTradingEngine:
    """Add monitoring capabilities to an existing trading engine.

    Args:
        engine: The trading engine to monitor
        watch: Enable live watch mode
        refresh_rate: Refresh rate for watch mode in seconds

    Returns:
        A MonitoredTradingEngine that wraps the original engine

    Example:
        ```python
        # Create your trading engine
        engine = TradingEngine(data_source, strategy, trader)

        # Add monitoring
        monitored_engine = add_monitoring_to_engine(
            engine, watch=True, refresh_rate=1.0
        )

        # Start with monitoring
        await monitored_engine.start()
        ```
    """
    return MonitoredTradingEngine(
        engine=engine, refresh_rate=refresh_rate, enabled=watch
    )
