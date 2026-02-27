from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from coinjure.events.events import Event, PriceChangeEvent
from coinjure.strategy.strategy import Strategy, StrategyDecision
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide


class MeanRevDipV1(Strategy):
    """Buy-the-dip mean-reversion strategy for fine-grained prediction markets.

    Alpha source (discovered via `coinjure research signal-test`):
    - On the J.D. Vance 2028 market (561974): buying after a price drop >= 0.005
      over a single 5-min period, then holding up to 12 periods, yields
      57% win rate and profit factor 3.30 (7 triggers, PnL +0.0115 per $1 size).

    Signal logic:
    - Fires when `price_drop = prev_price - current_price >= drop_threshold`.
    - Enters long at `current_price + half_spread`.
    - Exits at `take_profit_pct` gain above entry, or after `max_hold` periods,
      whichever comes first.
    - Cooldown of `cooldown` periods between trades to avoid stacking.

    Constructor kwargs (pass via --strategy-kwargs-json):
        drop_threshold  (str): min absolute 1-period drop to trigger entry, default "0.005"
        take_profit_pct (str): exit when price rises this fraction above entry, default "0.004"
        half_spread     (str): half of synthetic spread at execution, default "0.001"
        trade_size      (str): dollar size per order, default "10"
        max_hold        (int): max periods to hold before forced exit, default 12
        cooldown        (int): periods to skip after each exit, default 3
    """

    def __init__(
        self,
        drop_threshold: str = '0.005',
        take_profit_pct: str = '0.004',
        half_spread: str = '0.001',
        trade_size: str = '10',
        max_hold: int = 12,
        cooldown: int = 3,
    ) -> None:
        self.drop_threshold = Decimal(drop_threshold)
        self.take_profit_pct = Decimal(take_profit_pct)
        self.half_spread = Decimal(half_spread)
        self.trade_size = Decimal(trade_size)
        self.max_hold = int(max_hold)
        self.cooldown = int(cooldown)

        self._prev_price: dict[str, Decimal | None] = defaultdict(lambda: None)
        self._entry_price: dict[str, Decimal | None] = {}  # None = no position
        self._entry_event: dict[str, int] = {}
        self._last_exit: dict[str, int] = defaultdict(int)
        self._event_count: dict[str, int] = defaultdict(int)

        self._decisions: list[StrategyDecision] = []
        self._stats: dict[str, int] = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'sells': 0,
            'holds': 0,
        }

    # ------------------------------------------------------------------

    def _record(
        self,
        name: str,
        action: str,
        executed: bool,
        reasoning: str,
        signals: dict[str, float],
    ) -> None:
        self._decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=name[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                signal_values=signals,
            )
        )
        self._stats['decisions'] += 1
        if action == 'HOLD':
            self._stats['holds'] += 1
            return
        if not executed:
            return
        self._stats['executed'] += 1
        if action == 'BUY_YES':
            self._stats['buy_yes'] += 1
        elif action == 'SELL_YES':
            self._stats['sells'] += 1

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        sym = event.ticker.symbol
        name = (event.ticker.name or sym)[:40]
        price = event.price

        self._event_count[sym] += 1
        ev_idx = self._event_count[sym]
        prev = self._prev_price[sym]
        self._prev_price[sym] = price

        if prev is None:
            return

        drop = prev - price  # positive = price fell
        in_pos = sym in self._entry_price
        entry_price = self._entry_price.get(sym)
        signals = {
            'price': float(price),
            'prev_price': float(prev),
            'drop': float(drop),
            'ev_idx': float(ev_idx),
        }

        # ---- Forced exit: take-profit or max_hold ----
        if in_pos and entry_price is not None:
            gain = (price - entry_price) / entry_price
            held = ev_idx - self._entry_event[sym]
            if gain >= self.take_profit_pct or held >= self.max_hold:
                position = trader.position_manager.get_position(event.ticker)
                qty = self.trade_size
                if position is not None and position.quantity > 0:
                    qty = min(self.trade_size, position.quantity)
                exit_limit = max(Decimal('0.01'), price - self.half_spread)
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=event.ticker,
                    limit_price=exit_limit,
                    quantity=qty,
                )
                executed = result.order is not None and result.order.filled_quantity > 0
                reason = (
                    f'take_profit gain={float(gain):.4f}>={float(self.take_profit_pct)}'
                    if gain >= self.take_profit_pct
                    else f'max_hold timeout held={held}'
                )
                self._record(name, 'SELL_YES', executed, reason, signals)
                if executed:
                    del self._entry_price[sym]
                    del self._entry_event[sym]
                    self._last_exit[sym] = ev_idx
                return

        # ---- Cooldown guard ----
        if (ev_idx - self._last_exit[sym]) < self.cooldown:
            self._record(name, 'HOLD', False, f'cooldown', signals)
            return

        # ---- Entry: buy the dip ----
        if not in_pos and drop >= self.drop_threshold:
            entry_limit = min(Decimal('0.99'), price + self.half_spread)
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=event.ticker,
                limit_price=entry_limit,
                quantity=self.trade_size,
            )
            executed = result.order is not None and result.order.filled_quantity > 0
            self._record(
                name,
                'BUY_YES',
                executed,
                f'dip drop={float(drop):.5f}>={float(self.drop_threshold)} prev={float(prev):.5f}',
                signals,
            )
            if executed:
                self._entry_price[sym] = entry_limit
                self._entry_event[sym] = ev_idx
            return

        self._record(
            name,
            'HOLD',
            False,
            f'no signal drop={float(drop):.5f}<{float(self.drop_threshold)}',
            signals,
        )

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self._decisions[-200:])

    def get_decision_stats(self) -> dict[str, int | float]:
        return dict(self._stats)
