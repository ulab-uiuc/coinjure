"""Momentum strategy for prediction market binary options.

Fires on PriceChangeEvent. Maintains a rolling deque(maxlen=window) per ticker.

    momentum = (latest_price - oldest_price) / oldest_price

- Enter BUY_YES when momentum > entry_threshold and no position
- Exit when momentum < exit_threshold OR timeout
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from decimal import Decimal

from pred_market_cli.events.events import Event, PriceChangeEvent
from pred_market_cli.ticker.ticker import Ticker
from pred_market_cli.trader.trader import Trader
from pred_market_cli.trader.types import TradeSide

from .strategy import Strategy, StrategyDecision


class MomentumStrategy(Strategy):
    def __init__(
        self,
        tickers: list[Ticker],
        window: int = 10,
        entry_threshold: float = 0.02,
        exit_threshold: float = -0.01,
        position_size: Decimal = Decimal('10'),
        max_hold_seconds: int = 600,
    ) -> None:
        self.tickers: set[str] = {t.symbol for t in tickers}
        self._ticker_map: dict[str, Ticker] = {t.symbol: t for t in tickers}
        self.window = window
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.position_size = position_size
        self.max_hold_seconds = max_hold_seconds
        self.logger = logging.getLogger(__name__)

        # Rolling price windows per ticker symbol
        self._price_windows: dict[str, deque[float]] = {}

        self.decisions: deque[StrategyDecision] = deque(maxlen=200)
        self.total_decisions: int = 0
        self.total_executed: int = 0
        self.total_buy_yes: int = 0
        self.total_holds: int = 0
        self.total_closes: int = 0

        # symbol → entry_time
        self._entries: dict[str, datetime] = {}
        self._closing_in_progress: set[str] = set()

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, PriceChangeEvent):
            return
        ticker = event.ticker
        if ticker.symbol not in self.tickers:
            return

        sym = ticker.symbol
        price = float(event.price)

        if sym not in self._price_windows:
            self._price_windows[sym] = deque(maxlen=self.window)
        self._price_windows[sym].append(price)

        window = self._price_windows[sym]
        if len(window) < self.window:
            return  # Not enough history

        oldest = window[0]
        if oldest == 0:
            return
        momentum = (price - oldest) / oldest

        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0
        now = datetime.now()

        if has_position:
            if sym in self._closing_in_progress:
                return
            entry_time = self._entries.get(sym)
            elapsed = (now - entry_time).total_seconds() if entry_time else 0

            should_exit = (
                momentum < self.exit_threshold or elapsed > self.max_hold_seconds
            )
            if should_exit:
                action = (
                    'CLOSE_TIMEOUT' if elapsed > self.max_hold_seconds else 'CLOSE_MOM'
                )
                self._closing_in_progress.add(sym)
                try:
                    bid = trader.market_data.get_best_bid(ticker)
                    if bid is None:
                        return
                    result = await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=bid.price,
                        quantity=position.quantity,
                    )
                    executed = result.order is not None
                    if executed:
                        self._entries.pop(sym, None)
                        self.total_executed += 1
                    self._record_decision(
                        ticker_name=ticker.name or sym,
                        action=action,
                        executed=executed,
                        reasoning=f'momentum={momentum:.4f}, elapsed={elapsed:.0f}s',
                        signal_values={'momentum': momentum},
                    )
                    self.total_closes += 1
                finally:
                    self._closing_in_progress.discard(sym)
        else:
            if momentum > self.entry_threshold:
                ask = trader.market_data.get_best_ask(ticker)
                if ask is None:
                    return
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=ask.price,
                    quantity=self.position_size,
                )
                executed = result.order is not None
                if executed:
                    self._entries[sym] = now
                    self.total_executed += 1
                    self.total_buy_yes += 1
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='BUY_YES',
                    executed=executed,
                    reasoning=f'momentum={momentum:.4f} > threshold={self.entry_threshold}',
                    signal_values={'momentum': momentum},
                )
            else:
                self.total_holds += 1
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='HOLD',
                    executed=False,
                    reasoning=f'momentum={momentum:.4f}',
                    signal_values={'momentum': momentum},
                )

    def _record_decision(
        self,
        ticker_name: str,
        action: str,
        executed: bool,
        reasoning: str,
        signal_values: dict[str, float],
    ) -> None:
        self.decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=ticker_name[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                signal_values=signal_values,
            )
        )
        self.total_decisions += 1

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self.decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        return {
            'decisions': self.total_decisions,
            'executed': self.total_executed,
            'buy_yes': self.total_buy_yes,
            'holds': self.total_holds,
            'closes': self.total_closes,
        }
