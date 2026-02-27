"""Trend-following momentum strategy for high-volume, directionally drifting markets."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal

from coinjure.events.events import Event, PriceChangeEvent
from coinjure.strategy.strategy import Strategy, StrategyDecision
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide


class TrendMomentumV1(Strategy):
    """N-bar momentum strategy: enter on sustained consecutive up-moves, exit on reversal.

    Targets high-volume markets with clear directional drift. Long-only (buys YES tokens).

    Parameters
    ----------
    min_move:   minimum per-bar price change to qualify as an up-move (default 0.003)
    window:     number of consecutive up-moves required before entry (default 2)
    trade_size: USDC to stake per entry (default 10)
    max_hold:   max ticks to hold before forced exit (default 24)
    cooldown:   ticks to wait after an exit before re-entering on same ticker (default 4)
    """

    def __init__(
        self,
        min_move: float = 0.003,
        window: int = 2,
        trade_size: float = 10.0,
        max_hold: int = 24,
        cooldown: int = 4,
    ) -> None:
        self.min_move = Decimal(str(min_move))
        self.window = window
        self.trade_size = Decimal(str(trade_size))
        self.max_hold = max_hold
        self.cooldown = cooldown

        self._prices: dict[str, deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self.window + 1)
        )
        self._entry_event: dict[str, int] = {}
        self._last_exit_event: dict[str, int] = {}
        self._event_count = 0

        self._decisions: deque[StrategyDecision] = deque(maxlen=500)
        self._stats: dict[str, int] = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'buy_no': 0,
            'sells': 0,
            'closes': 0,
            'holds': 0,
        }

    def _record(
        self,
        *,
        ticker_name: str,
        action: str,
        executed: bool,
        reasoning: str,
        signal_values: dict | None = None,
    ) -> None:
        self._stats['decisions'] += 1
        if executed:
            self._stats['executed'] += 1
            if action == 'BUY_YES':
                self._stats['buy_yes'] += 1
            elif action in ('SELL_YES', 'CLOSE'):
                self._stats['sells'] += 1
        elif action == 'HOLD':
            self._stats['holds'] += 1

        self._decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=ticker_name[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                signal_values=signal_values or {},
            )
        )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, PriceChangeEvent):
            return

        self._event_count += 1
        ticker = event.ticker
        key = ticker.symbol
        price = event.price
        ticker_name = ticker.name or key

        history = self._prices[key]
        history.append(price)

        # --- exit logic ---
        if key in self._entry_event:
            held = self._event_count - self._entry_event[key]
            position = trader.position_manager.get_position(ticker)
            qty = position.quantity if position else Decimal('0')

            reversed_ = (
                len(history) >= 2 and (history[-2] - history[-1]) >= self.min_move
            )
            timeout = held >= self.max_hold

            if qty > 0 and (reversed_ or timeout):
                limit = max(Decimal('0.01'), price - Decimal('0.003'))
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=ticker,
                    limit_price=limit,
                    quantity=qty,
                )
                executed = result.order is not None and result.order.filled_quantity > 0
                if executed:
                    self._entry_event.pop(key, None)
                    self._last_exit_event[key] = self._event_count
                self._record(
                    ticker_name=ticker_name,
                    action='SELL_YES' if executed else 'HOLD',
                    executed=executed,
                    reasoning=f'exit reversed={reversed_} timeout={timeout} held={held}',
                    signal_values={'price': float(price), 'held': float(held)},
                )
                return

        # --- need enough bars ---
        if len(history) < self.window + 1:
            return

        # --- cooldown gate ---
        cooldown_left = self.cooldown - (
            self._event_count - self._last_exit_event.get(key, -(10**9))
        )
        if cooldown_left > 0:
            return

        # --- entry: all consecutive moves in window must be >= min_move ---
        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0
        if has_position or key in self._entry_event:
            return

        prices_list = list(history)
        moves = [
            prices_list[i] - prices_list[i - 1] for i in range(1, len(prices_list))
        ]
        all_up = all(m >= self.min_move for m in moves[-self.window :])

        if all_up and price > 0:
            qty = self.trade_size / price
            limit = min(Decimal('0.99'), price + Decimal('0.003'))
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=ticker,
                limit_price=limit,
                quantity=qty,
            )
            executed = result.order is not None and result.order.filled_quantity > 0
            if executed:
                self._entry_event[key] = self._event_count
            self._record(
                ticker_name=ticker_name,
                action='BUY_YES' if executed else 'HOLD',
                executed=executed,
                reasoning=f'{self.window}-bar momentum entry',
                signal_values={
                    'move_sum': float(sum(moves[-self.window :])),
                    'price': float(price),
                },
            )

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self._decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        return dict(self._stats)
