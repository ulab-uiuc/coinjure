"""Parity Arbitrage Strategy — exploit YES + NO != $1.00 mispricings.

In any binary prediction market, buying YES and NO should cost exactly $1.00.
When YES_ask + NO_ask < $1.00, buying both guarantees a risk-free profit.
For multi-outcome markets, the sum of all outcome asks must equal $1.00.

IMDEA/Flashbots (2025) documented $28.4M extracted via this strategy on
Polymarket in a single year:
  - Single-condition long arb: $5.9M
  - Single-condition short arb: $4.7M
  - Multi-condition YES long: $11.1M
  - Multi-condition NO long: $17.3M

Window of opportunity: ~200ms on liquid markets.  This strategy scans
on every OrderBookEvent and executes immediately when edges are found.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from coinjure.events.events import Event, OrderBookEvent
from coinjure.strategy.quant_strategy import QuantStrategy
from coinjure.ticker.ticker import PolyMarketTicker, Ticker
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide

logger = logging.getLogger(__name__)


@dataclass
class ParityOpportunity:
    """A detected parity mispricing."""

    yes_ticker: Ticker
    no_ticker: Ticker
    yes_ask: Decimal
    no_ask: Decimal
    combined_cost: Decimal  # YES_ask + NO_ask
    edge: Decimal  # 1.00 - combined_cost
    edge_pct: float  # as percentage
    market_name: str = ''


class ParityArbStrategy(QuantStrategy):
    """Detect and trade YES + NO < $1.00 mispricings.

    Parameters:
        trade_size: Dollar amount per leg.
        min_edge: Minimum edge (1.00 - YES_ask - NO_ask) to trigger trade.
        cooldown: Seconds between trades on the same market pair.
        max_concurrent: Maximum concurrent parity positions.
    """

    name: ClassVar[str] = 'parity_arb'
    version: ClassVar[str] = '0.1.0'
    author: ClassVar[str] = 'coinjure-arb'
    strategy_type: ClassVar[str] = 'quant'

    def __init__(
        self,
        trade_size: Decimal = Decimal('10'),
        min_edge: Decimal = Decimal('0.03'),
        cooldown: int = 300,
        max_concurrent: int = 20,
    ) -> None:
        super().__init__()
        self.trade_size = trade_size
        self.min_edge = min_edge
        self.cooldown = cooldown
        self.max_concurrent = max_concurrent

        self._last_trade_time: dict[str, float] = {}
        self._open_positions: int = 0
        self._total_arb_profit = Decimal('0')
        self._total_arb_trades = 0

    # ------------------------------------------------------------------
    # Core event handler
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        self.bind_context(event, trader)

        if self.is_paused():
            return

        if not isinstance(event, OrderBookEvent):
            return

        # Every orderbook update triggers a parity check on that market
        await self._check_and_trade(event.ticker, trader)

    # ------------------------------------------------------------------
    # Parity detection
    # ------------------------------------------------------------------

    async def _check_and_trade(self, ticker: Ticker, trader: Trader) -> None:
        """Check parity for the market associated with this ticker."""
        # Only process PolyMarketTickers that have a complement (NO side)
        if not isinstance(ticker, PolyMarketTicker):
            return

        # Normalise to YES ticker
        if not ticker.is_yes:
            # This is a NO ticker event — find the YES ticker
            yes_ticker = self._find_complement(ticker, trader)
            if yes_ticker is None:
                return
            no_ticker = ticker
        else:
            yes_ticker = ticker
            no_ticker_obj = ticker.get_no_ticker()
            if no_ticker_obj is None:
                return
            no_ticker = no_ticker_obj

        # Get best asks
        yes_ask_level = trader.market_data.get_best_ask(yes_ticker)
        no_ask_level = trader.market_data.get_best_ask(no_ticker)
        if yes_ask_level is None or no_ask_level is None:
            return

        yes_ask = yes_ask_level.price
        no_ask = no_ask_level.price
        combined = yes_ask + no_ask

        edge = Decimal('1') - combined
        if edge < self.min_edge:
            return

        # Cooldown check
        pair_key = f'{yes_ticker.symbol}|{no_ticker.symbol}'
        now = time.time()
        if now - self._last_trade_time.get(pair_key, 0) < self.cooldown:
            return

        if self._open_positions >= self.max_concurrent:
            return

        opp = ParityOpportunity(
            yes_ticker=yes_ticker,
            no_ticker=no_ticker,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined,
            edge=edge,
            edge_pct=float(edge) * 100,
            market_name=getattr(yes_ticker, 'name', '') or yes_ticker.symbol,
        )

        await self._execute_parity_arb(opp, trader, pair_key)

    async def _execute_parity_arb(
        self,
        opp: ParityOpportunity,
        trader: Trader,
        pair_key: str,
    ) -> None:
        """Buy both YES and NO to lock in the edge."""
        # Calculate quantity: trade_size spread across both legs
        leg_budget = self.trade_size / Decimal('2')

        yes_qty = (leg_budget / opp.yes_ask).quantize(Decimal('1'))
        no_qty = (leg_budget / opp.no_ask).quantize(Decimal('1'))
        qty = min(yes_qty, no_qty)  # equal shares on both sides
        if qty <= 0:
            return

        # Execute YES leg
        yes_result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=opp.yes_ticker,
            limit_price=opp.yes_ask,
            quantity=qty,
        )

        if not yes_result.executed:
            self.record_decision(
                ticker_name=opp.market_name,
                action='BUY_YES',
                executed=False,
                reasoning=f'Parity arb YES leg failed: edge={opp.edge_pct:.2f}%',
                signal_values={
                    'yes_ask': float(opp.yes_ask),
                    'no_ask': float(opp.no_ask),
                    'edge': float(opp.edge),
                },
            )
            return

        # Execute NO leg
        no_result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=opp.no_ticker,
            limit_price=opp.no_ask,
            quantity=qty,
        )

        both_executed = yes_result.executed and no_result.executed
        locked_profit = opp.edge * qty if both_executed else Decimal('0')

        self.record_decision(
            ticker_name=opp.market_name,
            action='PARITY_ARB',
            executed=both_executed,
            confidence=float(opp.edge),
            reasoning=(
                f'Parity arb: YES@{opp.yes_ask} + NO@{opp.no_ask} = '
                f'{opp.combined_cost} (edge={opp.edge_pct:.2f}%, '
                f'qty={qty}, locked_profit=${locked_profit:.2f})'
            ),
            signal_values={
                'yes_ask': float(opp.yes_ask),
                'no_ask': float(opp.no_ask),
                'combined_cost': float(opp.combined_cost),
                'edge': float(opp.edge),
                'edge_pct': opp.edge_pct,
                'quantity': float(qty),
                'locked_profit': float(locked_profit),
            },
        )

        if both_executed:
            self._last_trade_time[pair_key] = time.time()
            self._open_positions += 1
            self._total_arb_profit += locked_profit
            self._total_arb_trades += 1
            logger.info(
                'PARITY ARB: %s | YES@%.3f + NO@%.3f = %.3f | '
                'edge=%.2f%% | qty=%s | profit=$%.3f',
                opp.market_name,
                opp.yes_ask,
                opp.no_ask,
                opp.combined_cost,
                opp.edge_pct,
                qty,
                locked_profit,
            )
        elif yes_result.executed and not no_result.executed:
            logger.warning(
                'PARITY ARB PARTIAL: %s | YES filled but NO failed! '
                'Open directional risk.',
                opp.market_name,
            )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _find_complement(
        self, no_ticker: PolyMarketTicker, trader: Trader
    ) -> PolyMarketTicker | None:
        """Given a NO ticker, find its YES complement in the order book."""
        if no_ticker.no_token_id:
            # The NO ticker's no_token_id points back to the YES token
            for t in trader.market_data.order_books:
                if isinstance(t, PolyMarketTicker) and t.token_id == no_ticker.no_token_id:
                    return t
        return None

    def get_arb_stats(self) -> dict[str, object]:
        """Return running arbitrage statistics."""
        return {
            'total_arb_trades': self._total_arb_trades,
            'total_arb_profit': float(self._total_arb_profit),
            'open_positions': self._open_positions,
        }
