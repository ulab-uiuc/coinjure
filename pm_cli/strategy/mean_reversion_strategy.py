"""Mean reversion strategy for prediction market binary options.

Fires on PriceChangeEvent. Uses statistics.mean + statistics.stdev over rolling window.

    z_score = (price - mean) / std

- Enter long (BUY_YES) when z_score < -entry_z_score
- Enter short (SELL YES) only when z_score > +entry_z_score AND ticker has existing YES position
- Exit when abs(z_score) < exit_z_score OR timeout
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from datetime import datetime
from decimal import Decimal

from pred_market_cli.events.events import Event, PriceChangeEvent
from pred_market_cli.ticker.ticker import Ticker
from pred_market_cli.trader.trader import Trader
from pred_market_cli.trader.types import TradeSide

from .strategy import Strategy, StrategyDecision


class MeanReversionStrategy(Strategy):
    def __init__(
        self,
        tickers: list[Ticker],
        window: int = 20,
        entry_z_score: float = 1.5,
        exit_z_score: float = 0.5,
        position_size: Decimal = Decimal('10'),
        max_hold_seconds: int = 600,
    ) -> None:
        if window < 2:
            raise ValueError('window must be >= 2 for standard deviation calculation')
        self.tickers: set[str] = {t.symbol for t in tickers}
        self._ticker_map: dict[str, Ticker] = {t.symbol: t for t in tickers}
        self.window = window
        self.entry_z_score = entry_z_score
        self.exit_z_score = exit_z_score
        self.position_size = position_size
        self.max_hold_seconds = max_hold_seconds
        self.logger = logging.getLogger(__name__)

        # Rolling price windows per ticker symbol
        self._price_windows: dict[str, deque[float]] = {}

        self.decisions: deque[StrategyDecision] = deque(maxlen=200)
        self.total_decisions: int = 0
        self.total_executed: int = 0
        self.total_buy_yes: int = 0
        self.total_sell_yes: int = 0
        self.total_holds: int = 0
        self.total_closes: int = 0

        # symbol → (entry_time, side)
        self._entries: dict[str, tuple[datetime, str]] = {}
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

        window_data = list(self._price_windows[sym])
        if len(window_data) < self.window:
            return  # Not enough history

        mean = statistics.mean(window_data)
        std = statistics.stdev(window_data)

        if std == 0:
            return  # No variance — skip

        z_score = (price - mean) / std
        now = datetime.now()

        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0

        if has_position:
            if sym in self._closing_in_progress:
                return
            entry_meta = self._entries.get(sym)
            entry_time = entry_meta[0] if entry_meta else now
            elapsed = (now - entry_time).total_seconds()

            # Overbought relative to rolling mean: trim/close YES exposure.
            if z_score > self.entry_z_score and position.quantity > 0:
                bid = trader.market_data.get_best_bid(ticker)
                if bid is None:
                    return
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=ticker,
                    limit_price=bid.price,
                    quantity=min(self.position_size, position.quantity),
                )
                executed = result.order is not None
                if executed:
                    self.total_executed += 1
                    self.total_sell_yes += 1
                    updated = trader.position_manager.get_position(ticker)
                    if updated is None or updated.quantity <= 0:
                        self._entries.pop(sym, None)
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='SELL_YES',
                    executed=executed,
                    reasoning=f'z={z_score:.2f} > {self.entry_z_score}, mean={mean:.3f}',
                    signal_values={'z_score': z_score, 'mean': mean, 'std': std},
                )
                return

            should_exit = (
                abs(z_score) < self.exit_z_score or elapsed > self.max_hold_seconds
            )
            if should_exit:
                action = (
                    'CLOSE_TIMEOUT' if elapsed > self.max_hold_seconds else 'CLOSE_MR'
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
                        reasoning=f'z={z_score:.2f}, mean={mean:.3f}, std={std:.3f}',
                        signal_values={'z_score': z_score, 'mean': mean, 'std': std},
                    )
                    self.total_closes += 1
                finally:
                    self._closing_in_progress.discard(sym)
        else:
            if z_score < -self.entry_z_score:
                # Price far below mean — expect reversion upward → BUY YES
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
                    self._entries[sym] = (now, 'long')
                    self.total_executed += 1
                    self.total_buy_yes += 1
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='BUY_YES',
                    executed=executed,
                    reasoning=f'z={z_score:.2f} < -{self.entry_z_score}, mean={mean:.3f}',
                    signal_values={'z_score': z_score, 'mean': mean, 'std': std},
                )
            elif z_score > self.entry_z_score:
                # No position to short against — HOLD
                self.total_holds += 1
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='HOLD',
                    executed=False,
                    reasoning=f'z={z_score:.2f} > {self.entry_z_score} but no position to sell',
                    signal_values={'z_score': z_score, 'mean': mean, 'std': std},
                )
            else:
                self.total_holds += 1
                self._record_decision(
                    ticker_name=ticker.name or sym,
                    action='HOLD',
                    executed=False,
                    reasoning=f'z={z_score:.2f} within bounds',
                    signal_values={'z_score': z_score, 'mean': mean, 'std': std},
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
            'sell_yes': self.total_sell_yes,
            'holds': self.total_holds,
            'closes': self.total_closes,
        }
