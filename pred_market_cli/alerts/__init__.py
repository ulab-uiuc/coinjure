"""Alerts package — notification system for trading events."""

from pred_market_cli.alerts.alerter import Alerter, CompositeAlerter, LogAlerter
from pred_market_cli.alerts.telegram_alerter import TelegramAlerter

__all__ = ['Alerter', 'LogAlerter', 'TelegramAlerter', 'CompositeAlerter']
