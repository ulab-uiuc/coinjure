"""Automated monitoring and retirement for spread strategies.

Provides continuous checks on:
- P&L degradation (drawdown, consecutive losses)
- Relation validity (correlation breakdown, cointegration loss)
- Paper-to-live drift detection
- Stale data detection

Can auto-retire strategies when conditions are breached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MonitorConfig:
    """Configuration for automated strategy monitoring."""

    # P&L thresholds
    max_drawdown_pct: float = 0.15
    max_consecutive_losses: int = 10
    min_sharpe: float = -0.5

    # Relation validity
    min_correlation: float = 0.3
    max_correlation_drop: float = 0.3  # alert if corr drops by this much
    revalidation_interval_hours: float = 24.0

    # Data staleness
    max_stale_seconds: float = 300.0  # 5 minutes

    # Paper-live drift
    max_pnl_drift_pct: float = 0.20  # alert if live PnL deviates >20% from paper


@dataclass
class MonitorAlert:
    """An alert from the monitoring system."""

    alert_type: str  # drawdown, stale_data, correlation_break, drift, etc.
    severity: str  # info, warning, critical
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    action_taken: str = ''  # e.g., 'paused', 'retired', 'none'


class StrategyMonitor:
    """Monitor a running strategy and generate alerts / auto-actions.

    Usage::

        monitor = StrategyMonitor(config)
        alerts = monitor.check_pnl(equity_curve, initial_capital)
        alerts += monitor.check_staleness(last_event_time)
        alerts += monitor.check_relation_validity(corr_now, corr_initial)
    """

    def __init__(self, config: MonitorConfig | None = None) -> None:
        self._config = config or MonitorConfig()
        self._alerts: list[MonitorAlert] = []

    @property
    def alerts(self) -> list[MonitorAlert]:
        return list(self._alerts)

    def clear_alerts(self) -> None:
        self._alerts.clear()

    def check_pnl(
        self,
        equity_curve: list[float],
        initial_capital: float,
    ) -> list[MonitorAlert]:
        """Check P&L metrics for breaches."""
        alerts: list[MonitorAlert] = []

        if not equity_curve:
            return alerts

        current = equity_curve[-1]
        peak = max(equity_curve)

        # Drawdown check
        if peak > 0:
            drawdown = (peak - current) / peak
            if drawdown >= self._config.max_drawdown_pct:
                alert = MonitorAlert(
                    alert_type='drawdown',
                    severity='critical',
                    message=f'Drawdown {drawdown:.1%} exceeds limit {self._config.max_drawdown_pct:.1%}',
                    details={'drawdown': drawdown, 'peak': peak, 'current': current},
                )
                alerts.append(alert)

        # Consecutive losses
        if len(equity_curve) >= 2:
            losses = 0
            for i in range(len(equity_curve) - 1, 0, -1):
                if equity_curve[i] < equity_curve[i - 1]:
                    losses += 1
                else:
                    break
            if losses >= self._config.max_consecutive_losses:
                alert = MonitorAlert(
                    alert_type='consecutive_losses',
                    severity='warning',
                    message=f'{losses} consecutive losing periods',
                    details={'consecutive_losses': losses},
                )
                alerts.append(alert)

        self._alerts.extend(alerts)
        return alerts

    def check_staleness(
        self,
        last_event_time: datetime | None,
    ) -> list[MonitorAlert]:
        """Check if data is stale (no events for too long)."""
        alerts: list[MonitorAlert] = []

        if last_event_time is None:
            return alerts

        now = datetime.now(timezone.utc)
        if last_event_time.tzinfo is None:
            last_event_time = last_event_time.replace(tzinfo=timezone.utc)

        seconds_stale = (now - last_event_time).total_seconds()
        if seconds_stale > self._config.max_stale_seconds:
            alert = MonitorAlert(
                alert_type='stale_data',
                severity='warning',
                message=f'No events for {seconds_stale:.0f}s (limit: {self._config.max_stale_seconds:.0f}s)',
                details={'seconds_stale': seconds_stale},
            )
            alerts.append(alert)

        self._alerts.extend(alerts)
        return alerts

    def check_relation_validity(
        self,
        current_correlation: float,
        initial_correlation: float,
    ) -> list[MonitorAlert]:
        """Check if a market relation is still valid."""
        alerts: list[MonitorAlert] = []

        # Absolute correlation floor
        if abs(current_correlation) < self._config.min_correlation:
            alert = MonitorAlert(
                alert_type='correlation_break',
                severity='critical',
                message=f'Correlation {current_correlation:.3f} below minimum {self._config.min_correlation}',
                details={
                    'current': current_correlation,
                    'minimum': self._config.min_correlation,
                },
            )
            alerts.append(alert)

        # Relative correlation drop
        if initial_correlation != 0:
            drop = abs(initial_correlation) - abs(current_correlation)
            if drop > self._config.max_correlation_drop:
                alert = MonitorAlert(
                    alert_type='correlation_degradation',
                    severity='warning',
                    message=f'Correlation dropped by {drop:.3f} (limit: {self._config.max_correlation_drop})',
                    details={
                        'initial': initial_correlation,
                        'current': current_correlation,
                        'drop': drop,
                    },
                )
                alerts.append(alert)

        self._alerts.extend(alerts)
        return alerts

    def check_paper_live_drift(
        self,
        paper_pnl: float,
        live_pnl: float,
    ) -> list[MonitorAlert]:
        """Check if live performance has drifted from paper trading."""
        alerts: list[MonitorAlert] = []

        if paper_pnl == 0:
            return alerts

        drift = abs(live_pnl - paper_pnl) / abs(paper_pnl)
        if drift > self._config.max_pnl_drift_pct:
            alert = MonitorAlert(
                alert_type='paper_live_drift',
                severity='warning',
                message=f'Live/paper PnL drift {drift:.1%} exceeds limit {self._config.max_pnl_drift_pct:.1%}',
                details={
                    'paper_pnl': paper_pnl,
                    'live_pnl': live_pnl,
                    'drift_pct': drift,
                },
            )
            alerts.append(alert)

        self._alerts.extend(alerts)
        return alerts

    def should_retire(self) -> tuple[bool, str]:
        """Check if any critical alerts warrant automatic retirement."""
        critical = [a for a in self._alerts if a.severity == 'critical']
        if critical:
            reasons = [a.alert_type for a in critical]
            return True, f'critical alerts: {", ".join(reasons)}'
        return False, ''
