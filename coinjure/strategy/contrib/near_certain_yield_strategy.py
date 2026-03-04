"""Near-Certain Yield Strategy — the "bond" strategy for prediction markets.

Scans all active markets for contracts trading above a high price threshold
(e.g. >$0.93), calculates annualised yield, and buys those that exceed a
minimum return — holding to resolution for near-guaranteed payoff.

Academic evidence (IMDEA/Flashbots 2025): >90% of orders exceeding $10K on
Polymarket are placed at price levels above $0.95, confirming institutional
adoption of this approach.

Risks:
- Black swan: a single "sure thing" resolving NO wipes many wins.
- Capital lockup: funds tied until resolution (days–months).
- Oracle risk: UMA disputes can flip seemingly settled outcomes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from coinjure.events.events import Event, OrderBookEvent
from coinjure.strategy.quant_strategy import QuantStrategy
from coinjure.ticker.ticker import Ticker
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide

logger = logging.getLogger(__name__)


@dataclass
class YieldOpportunity:
    """A near-certain contract worth buying."""

    ticker: Ticker
    side: str  # 'yes' or 'no'
    ask_price: float
    implied_yield: float  # raw (1/price - 1)
    annualised_yield: float  # yield * (365 / days_to_resolution)
    days_to_resolution: float
    market_name: str = ''


@dataclass
class HeldYieldPosition:
    """Tracks a position opened by this strategy."""

    ticker_symbol: str
    side: str
    entry_price: float
    entry_time: float  # epoch
    market_name: str = ''


class NearCertainYieldStrategy(QuantStrategy):
    """Buy high-probability contracts and hold to resolution.

    Parameters:
        trade_size: Dollar amount per position.
        min_price: Minimum ask price to consider (e.g. 0.93 = 93%).
        min_annualised_yield: Minimum annualised return (e.g. 0.10 = 10%).
        max_days: Maximum days to resolution.
        max_positions: Maximum concurrent yield positions.
        scan_cooldown: Seconds between full scans.
    """

    name: ClassVar[str] = 'near_certain_yield'
    version: ClassVar[str] = '0.1.0'
    author: ClassVar[str] = 'coinjure-arb'
    strategy_type: ClassVar[str] = 'quant'

    def __init__(
        self,
        trade_size: Decimal = Decimal('10'),
        min_price: float = 0.93,
        min_annualised_yield: float = 0.10,
        max_days: float = 90.0,
        max_positions: int = 10,
        scan_cooldown: int = 60,
    ) -> None:
        super().__init__()
        self.trade_size = trade_size
        self.min_price = min_price
        self.min_annualised_yield = min_annualised_yield
        self.max_days = max_days
        self.max_positions = max_positions
        self.scan_cooldown = scan_cooldown

        self._positions: dict[str, HeldYieldPosition] = {}
        self._last_scan: float = 0.0
        self._blacklist: set[str] = set()  # symbols to skip

    # ------------------------------------------------------------------
    # Core event handler
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.bind_context(event, trader)

        if self.is_paused():
            return

        # Scan on every OrderBookEvent, rate-limited
        if isinstance(event, OrderBookEvent):
            now = time.time()
            if now - self._last_scan < self.scan_cooldown:
                return
            self._last_scan = now
            await self._scan_and_trade(trader)

    # ------------------------------------------------------------------
    # Scanning logic
    # ------------------------------------------------------------------

    async def _scan_and_trade(self, trader: Trader) -> None:
        """Scan all visible markets for near-certain yield opportunities."""
        ctx = self.require_context()
        opportunities: list[YieldOpportunity] = []

        for ob_view in ctx.order_books():
            if ob_view.symbol in self._blacklist:
                continue
            if ob_view.symbol in self._positions:
                continue
            if ob_view.best_ask is None:
                continue

            ask = ob_view.best_ask
            if ask < self.min_price or ask >= 1.0:
                continue

            # Rough yield calculation
            raw_yield = (1.0 / ask) - 1.0
            # Assume ~30 days to resolution if we don't know the actual date
            # In production, this would come from market metadata
            days_est = 30.0

            annualised = raw_yield * (365.0 / days_est) if days_est > 0 else 0.0
            if annualised < self.min_annualised_yield:
                continue

            ticker = ctx.resolve_ticker(ob_view.symbol)
            if ticker is None:
                continue

            opportunities.append(
                YieldOpportunity(
                    ticker=ticker,
                    side='yes',
                    ask_price=ask,
                    implied_yield=raw_yield,
                    annualised_yield=annualised,
                    days_to_resolution=days_est,
                    market_name=ob_view.name,
                )
            )

        # Sort by annualised yield (highest first)
        opportunities.sort(key=lambda o: o.annualised_yield, reverse=True)

        # Execute top opportunities up to max_positions
        slots = self.max_positions - len(self._positions)
        for opp in opportunities[:slots]:
            await self._open_yield_position(opp, trader)

    async def _open_yield_position(
        self, opp: YieldOpportunity, trader: Trader
    ) -> None:
        """Buy a near-certain contract."""
        price = Decimal(str(opp.ask_price))
        if price <= 0:
            return

        quantity = self.trade_size / price
        quantity = quantity.quantize(Decimal('1'))  # whole shares
        if quantity <= 0:
            return

        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=opp.ticker,
            limit_price=price,
            quantity=quantity,
        )

        executed = result.executed
        self.record_decision(
            ticker_name=opp.market_name or opp.ticker.symbol,
            action='BUY_YES',
            executed=executed,
            confidence=opp.ask_price,
            reasoning=(
                f'Near-certain yield: ask={opp.ask_price:.2%}, '
                f'raw_yield={opp.implied_yield:.2%}, '
                f'ann_yield={opp.annualised_yield:.1%}'
            ),
            signal_values={
                'ask_price': opp.ask_price,
                'implied_yield': opp.implied_yield,
                'annualised_yield': opp.annualised_yield,
                'days_to_resolution': opp.days_to_resolution,
            },
        )

        if executed:
            self._positions[opp.ticker.symbol] = HeldYieldPosition(
                ticker_symbol=opp.ticker.symbol,
                side='yes',
                entry_price=opp.ask_price,
                entry_time=time.time(),
                market_name=opp.market_name,
            )
            logger.info(
                'Yield position opened: %s @ %.2f%% (ann %.1f%%)',
                opp.market_name or opp.ticker.symbol,
                opp.ask_price * 100,
                opp.annualised_yield * 100,
            )
