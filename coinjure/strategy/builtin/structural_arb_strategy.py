"""StructuralArbStrategy — trade deterministic structural constraint violations.

For structural relations with a known linear constraint between two markets:

    p(A) = slope × p(B) + intercept    (± tolerance)

Example: Two markets on the same underlying with different payout
structures, or markets with a known mathematical relationship.

When market prices deviate beyond tolerance from the expected relationship,
we trade the deviation back to the structural equilibrium.

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/structural_arb_strategy.py:StructuralArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "xxx", "slope": 1.0, "intercept": 0.0}'
"""

from __future__ import annotations

import logging
from decimal import Decimal

from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.relation_mixin import RelationArbMixin
from coinjure.strategy.strategy import Strategy
from coinjure.trading.sizing import compute_trade_size
from coinjure.trading.trader import Trader

logger = logging.getLogger(__name__)


class StructuralArbStrategy(RelationArbMixin, Strategy):
    """Arbitrage deviations from a deterministic structural constraint.

    The expected relationship is: p(A) = slope × p(B) + intercept.
    Trade when the residual (actual - expected) exceeds min_edge.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore.
    trade_size:
        Max dollar amount per leg.
    slope:
        Expected linear relationship slope (default 1.0).
    intercept:
        Expected linear relationship intercept (default 0.0).
    min_edge:
        Minimum residual to trigger entry.
    kelly_fraction:
        Conservative Kelly multiplier for dynamic sizing.
    """

    name = 'structural_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        slope: float = 1.0,
        intercept: float = 0.0,
        min_edge: float = 0.02,
        kelly_fraction: float = 0.1,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.max_trade_size = Decimal(str(trade_size))
        self.slope = slope
        self.intercept = intercept
        self.min_edge = Decimal(str(min_edge))
        self.kelly_fraction = Decimal(str(kelly_fraction))

        self._init_from_relation(relation_id)

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # flat | long_a_short_b (A underpriced) | short_a_long_b (A overpriced)
        self._position_state = 'flat'

    def reset_live_state(self) -> None:
        self._price_a = None
        self._price_b = None
        self._position_state = 'flat'
        self._owned_symbols = set()

    def _expected_a(self, price_b: float) -> float:
        """Compute expected p(A) from the structural relationship."""
        return self.slope * price_b + self.intercept

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        if ticker.side == 'no':
            return

        if self._slot_matches(ticker, 0):
            self._price_a = event.price
        elif self._slot_matches(ticker, 1):
            self._price_b = event.price
        else:
            return

        if self._price_a is None or self._price_b is None:
            return

        pa = float(self._price_a)
        pb = float(self._price_b)
        expected = self._expected_a(pb)
        residual = pa - expected

        if self._position_state == 'flat':
            if residual > float(self.min_edge):
                await self._enter_short_a(trader, pa, expected, residual)
            elif residual < -float(self.min_edge):
                await self._enter_long_a(trader, pa, expected, residual)
            else:
                self.record_decision(
                    ticker_name=f'struc({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={pa:.4f} expected={expected:.4f} '
                        f'residual={residual:.4f} within ±{float(self.min_edge):.4f}'
                    ),
                    signal_values={
                        'price_a': pa,
                        'price_b': pb,
                        'expected': expected,
                        'residual': residual,
                    },
                )
        else:
            if abs(residual) < float(self.min_edge) * 0.5:
                await self._exit(trader, residual)

    async def _enter_short_a(
        self,
        trader: Trader,
        pa: float,
        expected: float,
        residual: float,
    ) -> None:
        """A overpriced → sell A (buy NO), buy B (buy YES)."""
        ticker_a_no = self._find_ticker(trader, self._ids[0], side='no')
        ticker_b = self._find_ticker(trader, self._ids[1], side='yes')

        size = compute_trade_size(
            trader.position_manager, Decimal(str(abs(residual))),
            kelly_fraction=self.kelly_fraction,
            max_size=self.max_trade_size,
        )
        ok = await self._place_pair(
            trader,
            ticker_a_no, Decimal('1') - self._price_a if self._price_a else Decimal('0'),
            ticker_b, self._price_b or Decimal('0'),
            size,
        )
        if not ok:
            return

        self._position_state = 'short_a_long_b'
        self.record_decision(
            ticker_name=f'struc({self.relation_id[:20]})',
            action='ENTER_SHORT_A',
            executed=True,
            reasoning=f'A={pa:.4f} > expected={expected:.4f}, residual={residual:.4f}',
            signal_values={
                'price_a': pa,
                'price_b': float(self._price_b or 0),
                'expected': expected,
                'residual': residual,
            },
        )
        logger.info('ENTER structural arb: A overpriced, residual=%.4f', residual)

    async def _enter_long_a(
        self,
        trader: Trader,
        pa: float,
        expected: float,
        residual: float,
    ) -> None:
        """A underpriced → buy A (buy YES), sell B (buy NO)."""
        ticker_a = self._find_ticker(trader, self._ids[0], side='yes')
        ticker_b_no = self._find_ticker(trader, self._ids[1], side='no')

        size = compute_trade_size(
            trader.position_manager, Decimal(str(abs(residual))),
            kelly_fraction=self.kelly_fraction,
            max_size=self.max_trade_size,
        )
        ok = await self._place_pair(
            trader,
            ticker_a, self._price_a or Decimal('0'),
            ticker_b_no, Decimal('1') - self._price_b if self._price_b else Decimal('0'),
            size,
        )
        if not ok:
            return

        self._position_state = 'long_a_short_b'
        self.record_decision(
            ticker_name=f'struc({self.relation_id[:20]})',
            action='ENTER_LONG_A',
            executed=True,
            reasoning=f'A={pa:.4f} < expected={expected:.4f}, residual={residual:.4f}',
            signal_values={
                'price_a': pa,
                'price_b': float(self._price_b or 0),
                'expected': expected,
                'residual': residual,
            },
        )
        logger.info('ENTER structural arb: A underpriced, residual=%.4f', residual)

    async def _exit(self, trader: Trader, residual: float) -> None:
        await self._close_owned(trader)

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'struc({self.relation_id[:20]})',
            action='EXIT',
            executed=True,
            reasoning=f'Residual converged: {residual:.4f} (was {prev})',
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'residual': residual,
            },
        )
        logger.info('EXIT structural arb: residual converged')
