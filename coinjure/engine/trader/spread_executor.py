"""Atomic multi-leg spread execution layer.

Provides `SpreadExecutor` that wraps an underlying `Trader` and adds
multi-leg execution with partial-fill protection. If leg 1 fills but
leg 2 fails, it automatically hedges by unwinding leg 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from coinjure.engine.trader.trader import Trader
from coinjure.engine.trader.types import (
    OrderFailureReason,
    PlaceOrderResult,
    TradeSide,
)
from coinjure.ticker import Ticker

logger = logging.getLogger(__name__)


@dataclass
class SpreadLeg:
    """A single leg of a spread order."""

    side: TradeSide
    ticker: Ticker
    limit_price: Decimal
    quantity: Decimal


@dataclass
class SpreadOrderResult:
    """Result of a multi-leg spread execution."""

    success: bool
    leg_results: list[PlaceOrderResult]
    hedged: bool = False  # True if we had to unwind a partial fill
    failure_reason: str = ''

    @property
    def all_filled(self) -> bool:
        return all(
            r.order is not None and r.order.filled_quantity > 0
            for r in self.leg_results
        )


class SpreadExecutor:
    """Execute multi-leg spread orders with automatic hedge on partial fill.

    Usage::

        executor = SpreadExecutor(trader)
        result = await executor.execute_spread([
            SpreadLeg(TradeSide.BUY, ticker_a, ask_a, qty),
            SpreadLeg(TradeSide.SELL, ticker_b, bid_b, qty),
        ])
    """

    def __init__(self, trader: Trader) -> None:
        self._trader = trader

    async def execute_spread(
        self,
        legs: list[SpreadLeg],
        *,
        unwind_on_partial: bool = True,
    ) -> SpreadOrderResult:
        """Execute a multi-leg spread order sequentially.

        Legs are executed in order. If a leg fails and ``unwind_on_partial``
        is True, all previously filled legs are unwound (reversed).

        Args:
            legs: Ordered list of spread legs to execute.
            unwind_on_partial: If True, unwind filled legs on failure.

        Returns:
            SpreadOrderResult with per-leg results and hedge status.
        """
        if not legs:
            return SpreadOrderResult(
                success=False, leg_results=[], failure_reason='no legs'
            )

        results: list[PlaceOrderResult] = []
        filled_legs: list[tuple[SpreadLeg, PlaceOrderResult]] = []

        for i, leg in enumerate(legs):
            logger.info(
                'Spread leg %d/%d: %s %s qty=%s @ %s',
                i + 1,
                len(legs),
                leg.side.value,
                leg.ticker.symbol,
                leg.quantity,
                leg.limit_price,
            )

            result = await self._trader.place_order(
                side=leg.side,
                ticker=leg.ticker,
                limit_price=leg.limit_price,
                quantity=leg.quantity,
            )
            results.append(result)

            if result.order is not None and result.order.filled_quantity > 0:
                filled_legs.append((leg, result))
            else:
                # Leg failed — decide whether to unwind
                failure = result.failure_reason
                logger.warning(
                    'Spread leg %d failed: %s. Filled legs so far: %d',
                    i + 1,
                    failure,
                    len(filled_legs),
                )

                if unwind_on_partial and filled_legs:
                    logger.info('Unwinding %d filled legs', len(filled_legs))
                    await self._unwind(filled_legs)
                    return SpreadOrderResult(
                        success=False,
                        leg_results=results,
                        hedged=True,
                        failure_reason=f'leg {i + 1} failed: {failure}',
                    )

                return SpreadOrderResult(
                    success=False,
                    leg_results=results,
                    hedged=False,
                    failure_reason=f'leg {i + 1} failed: {failure}',
                )

        return SpreadOrderResult(success=True, leg_results=results)

    async def _unwind(
        self,
        filled_legs: list[tuple[SpreadLeg, PlaceOrderResult]],
    ) -> None:
        """Reverse all filled legs to hedge out the partial spread."""
        for leg, result in reversed(filled_legs):
            if result.order is None:
                continue

            filled_qty = result.order.filled_quantity
            if filled_qty <= 0:
                continue

            # Reverse the side
            reverse_side = (
                TradeSide.SELL if leg.side == TradeSide.BUY else TradeSide.BUY
            )

            # Use market price for urgency
            if reverse_side == TradeSide.SELL:
                bid = self._trader.market_data.get_best_bid(leg.ticker)
                price = bid.price if bid is not None else leg.limit_price
            else:
                ask = self._trader.market_data.get_best_ask(leg.ticker)
                price = ask.price if ask is not None else leg.limit_price

            logger.info(
                'Unwinding: %s %s qty=%s @ %s',
                reverse_side.value,
                leg.ticker.symbol,
                filled_qty,
                price,
            )

            try:
                await self._trader.place_order(
                    side=reverse_side,
                    ticker=leg.ticker,
                    limit_price=price,
                    quantity=filled_qty,
                )
            except Exception:
                logger.exception(
                    'Failed to unwind leg %s — MANUAL INTERVENTION REQUIRED',
                    leg.ticker.symbol,
                )

    async def execute_pair_spread(
        self,
        buy_ticker: Ticker,
        sell_ticker: Ticker,
        quantity: Decimal,
        *,
        unwind_on_partial: bool = True,
    ) -> SpreadOrderResult:
        """Convenience method for a simple 2-leg spread (buy A, sell B).

        Automatically gets best ask/bid for pricing.
        """
        ask = self._trader.market_data.get_best_ask(buy_ticker)
        bid = self._trader.market_data.get_best_bid(sell_ticker)

        if ask is None:
            return SpreadOrderResult(
                success=False,
                leg_results=[],
                failure_reason=f'no ask for {buy_ticker.symbol}',
            )
        if bid is None:
            return SpreadOrderResult(
                success=False,
                leg_results=[],
                failure_reason=f'no bid for {sell_ticker.symbol}',
            )

        legs = [
            SpreadLeg(TradeSide.BUY, buy_ticker, ask.price, quantity),
            SpreadLeg(TradeSide.SELL, sell_ticker, bid.price, quantity),
        ]

        return await self.execute_spread(legs, unwind_on_partial=unwind_on_partial)
