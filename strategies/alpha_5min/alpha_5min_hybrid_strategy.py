from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal

from coinjure.events.events import Event, PriceChangeEvent
from coinjure.strategy.strategy import Strategy, StrategyDecision
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide


class AlphaFiveMinHybridStrategy(Strategy):
    """Short-horizon hybrid strategy: momentum entries + mean-reversion entries."""

    def __init__(
        self,
        trade_size: str | float | int = '25',
        lookback: int = 5,
        momentum_entry: str | float | int = '0.018',
        mean_revert_z: str | float | int = '1.25',
        stop_loss: str | float | int = '0.08',
        take_profit: str | float | int = '0.12',
        max_position: str | float | int = '100',
        slippage: str | float | int = '0.005',
    ) -> None:
        self.trade_size = Decimal(str(trade_size))
        self.lookback = max(3, int(lookback))
        self.momentum_entry = Decimal(str(momentum_entry))
        self.mean_revert_z = Decimal(str(mean_revert_z))
        self.stop_loss = Decimal(str(stop_loss))
        self.take_profit = Decimal(str(take_profit))
        self.max_position = Decimal(str(max_position))
        self.slippage = Decimal(str(slippage))

        self._history: dict[str, deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self.lookback + 3)
        )
        self._entry_price: dict[str, Decimal] = {}
        self._decisions: deque[StrategyDecision] = deque(maxlen=200)
        self._stats = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'sell_yes': 0,
            'holds': 0,
        }

    @staticmethod
    def _as_decimal(value: Decimal | float) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _mean(values: list[Decimal]) -> Decimal:
        return sum(values, Decimal('0')) / Decimal(len(values))

    @staticmethod
    def _stddev(values: list[Decimal], mean: Decimal) -> Decimal:
        if not values:
            return Decimal('0')
        variance = sum((v - mean) ** 2 for v in values) / Decimal(len(values))
        return variance.sqrt() if variance > 0 else Decimal('0')

    def _record(self, action: str, executed: bool, event: PriceChangeEvent, reason: str) -> None:
        if action == 'HOLD':
            self._stats['holds'] += 1
        if executed:
            self._stats['executed'] += 1
            if action == 'BUY_YES':
                self._stats['buy_yes'] += 1
            elif action == 'SELL_YES':
                self._stats['sell_yes'] += 1
        self._stats['decisions'] += 1
        self._decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=(event.ticker.name or event.ticker.symbol)[:40],
                action=action,
                executed=executed,
                reasoning=reason,
                signal_values={'price': float(event.price)},
            )
        )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if isinstance(event, PriceChangeEvent):
            symbol = event.ticker.symbol
            px = event.price
            if px <= 0:
                return
            series = self._history[symbol]
            prev = series[-1] if series else None
            series.append(px)
            if prev is None:
                return

            move = (px / prev) - Decimal('1')
            position = trader.position_manager.get_position(event.ticker)
            qty = position.quantity if position is not None else Decimal('0')
            has_pos = qty > 0

            rolling = list(series)[-self.lookback :]
            mean = self._mean(rolling)
            std = self._stddev(rolling, mean)
            z = Decimal('0') if std == 0 else (px - mean) / std

            action = 'HOLD'
            executed = False
            reason = f'move={float(move):+.3%}, z={float(z):+.2f}'

            if has_pos:
                entry = self._entry_price.get(symbol, mean)
                pnl = (px / entry) - Decimal('1') if entry > 0 else Decimal('0')
                exit_on_risk = pnl <= -self.stop_loss or pnl >= self.take_profit
                exit_on_reversal = z >= self.mean_revert_z and move < 0
                if exit_on_risk or exit_on_reversal:
                    sell_qty = min(self.trade_size, qty)
                    if sell_qty > 0:
                        limit_price = max(Decimal('0.01'), px - self.slippage)
                        result = await trader.place_order(
                            side=TradeSide.SELL,
                            ticker=event.ticker,
                            limit_price=limit_price,
                            quantity=sell_qty,
                        )
                        executed = (
                            result.order is not None
                            and result.order.filled_quantity > 0
                        )
                        action = 'SELL_YES'
                        reason = (
                            f'Exit: pnl={float(pnl):+.3%}, z={float(z):+.2f}'
                        )
                        if executed and qty - sell_qty <= 0:
                            self._entry_price.pop(symbol, None)
            else:
                mean_revert_entry = z <= -self.mean_revert_z and move > 0
                momentum_entry = move >= self.momentum_entry and z > 0
                if mean_revert_entry or momentum_entry:
                    buy_qty = min(self.trade_size, self.max_position - qty)
                    if buy_qty > 0 and px < Decimal('0.99'):
                        limit_price = min(Decimal('0.99'), px + self.slippage)
                        result = await trader.place_order(
                            side=TradeSide.BUY,
                            ticker=event.ticker,
                            limit_price=limit_price,
                            quantity=buy_qty,
                        )
                        executed = (
                            result.order is not None
                            and result.order.filled_quantity > 0
                        )
                        action = 'BUY_YES'
                        if executed:
                            self._entry_price[symbol] = px
                        trigger = 'revert' if mean_revert_entry else 'momentum'
                        reason = f'Entry({trigger}): move={float(move):+.3%}, z={float(z):+.2f}'

            self._record(action, executed, event, reason)
            return

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self._decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        return dict(self._stats)
