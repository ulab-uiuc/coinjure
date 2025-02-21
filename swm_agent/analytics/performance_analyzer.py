from dataclasses import dataclass
from decimal import Decimal

from trader.types import Trade


@dataclass
class TradeStats:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    average_profit: Decimal
    average_loss: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal


class PerformanceAnalyzer:
    def __init__(self) -> None:
        self.trades: list[Trade] = []

    def add_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        self._update_metrics()

    def _update_metrics(self) -> None:
        pass
