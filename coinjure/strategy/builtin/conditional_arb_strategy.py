"""ConditionalArbStrategy — trade conditional probability constraint violations.

For conditional relations where p(A|B) is bounded, the joint pricing of
A and B is constrained:

    p(A) ≥ cond_lower × p(B)
    p(A) ≤ cond_upper × p(B) + (1 - p(B))

Example: "ceasefire by June" is conditional on "peace talks by March".
If p(ceasefire|talks) ∈ [0.4, 0.9], and talks = 0.6, then:
    ceasefire should be in [0.24, 0.94]

When market prices violate these bounds, we trade:
  - p(A) too high: sell A, buy B
  - p(A) too low: buy A, sell B

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/conditional_arb_strategy.py:ConditionalArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "xxx", "cond_lower": 0.4, "cond_upper": 0.9}'
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


class ConditionalArbStrategy(RelationArbMixin, Strategy):
    """Arbitrage conditional probability constraint violations.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore.
    trade_size:
        Max dollar amount per leg.
    cond_lower:
        Lower bound on p(A|B). Default 0 (no lower bound).
    cond_upper:
        Upper bound on p(A|B). Default 1 (no upper bound).
    min_edge:
        Minimum distance outside the band to trigger entry.
    kelly_fraction:
        Conservative Kelly multiplier for dynamic sizing.
    """

    name = 'conditional_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        cond_lower: float = 0.0,
        cond_upper: float = 1.0,
        min_edge: float = 0.02,
        kelly_fraction: float = 0.1,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.max_trade_size = Decimal(str(trade_size))
        self.cond_lower = cond_lower
        self.cond_upper = cond_upper
        self.min_edge = Decimal(str(min_edge))
        self.kelly_fraction = Decimal(str(kelly_fraction))

        self._init_from_relation(relation_id)

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # flat | long_a_short_b (A too low) | short_a_long_b (A too high)
        self._position_state = 'flat'

    def reset_live_state(self) -> None:
        self._price_a = None
        self._price_b = None
        self._position_state = 'flat'
        self._owned_symbols = set()

    def _compute_bounds(self, price_b: float) -> tuple[float, float]:
        """Compute the valid range for p(A) given p(B) and conditional bounds."""
        lower = self.cond_lower * price_b
        upper = self.cond_upper * price_b + (1 - price_b)
        return lower, upper

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
        lower, upper = self._compute_bounds(pb)

        if self._position_state == 'flat':
            if pa > upper + float(self.min_edge):
                await self._enter_short_a(trader, pa, lower, upper)
            elif pa < lower - float(self.min_edge):
                await self._enter_long_a(trader, pa, lower, upper)
            else:
                self.record_decision(
                    ticker_name=f'cond({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={pa:.4f} in band [{lower:.4f}, {upper:.4f}] '
                        f'(B={pb:.4f})'
                    ),
                    signal_values={
                        'price_a': pa,
                        'price_b': pb,
                        'lower': lower,
                        'upper': upper,
                    },
                )
        else:
            if lower <= pa <= upper:
                await self._exit(trader, pa, lower, upper)

    async def _enter_short_a(
        self,
        trader: Trader,
        pa: float,
        lower: float,
        upper: float,
    ) -> None:
        """A too expensive → sell A (buy NO), buy B (buy YES)."""
        ticker_a_no = self._find_ticker(trader, self._ids[0], side='no')
        ticker_b = self._find_ticker(trader, self._ids[1], side='yes')

        edge = Decimal(str(pa - upper))
        size = compute_trade_size(
            trader.position_manager, edge,
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
            ticker_name=f'cond({self.relation_id[:20]})',
            action='ENTER_SHORT_A',
            executed=True,
            reasoning=f'A={pa:.4f} > upper={upper:.4f}: sell A, buy B',
            signal_values={
                'price_a': pa,
                'price_b': float(self._price_b or 0),
                'lower': lower,
                'upper': upper,
            },
        )
        logger.info('ENTER conditional arb: A too high, sell A buy B')

    async def _enter_long_a(
        self,
        trader: Trader,
        pa: float,
        lower: float,
        upper: float,
    ) -> None:
        """A too cheap → buy A (buy YES), sell B (buy NO)."""
        ticker_a = self._find_ticker(trader, self._ids[0], side='yes')
        ticker_b_no = self._find_ticker(trader, self._ids[1], side='no')

        edge = Decimal(str(lower - pa))
        size = compute_trade_size(
            trader.position_manager, edge,
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
            ticker_name=f'cond({self.relation_id[:20]})',
            action='ENTER_LONG_A',
            executed=True,
            reasoning=f'A={pa:.4f} < lower={lower:.4f}: buy A, sell B',
            signal_values={
                'price_a': pa,
                'price_b': float(self._price_b or 0),
                'lower': lower,
                'upper': upper,
            },
        )
        logger.info('ENTER conditional arb: A too low, buy A sell B')

    async def _exit(
        self,
        trader: Trader,
        pa: float,
        lower: float,
        upper: float,
    ) -> None:
        await self._close_owned(trader)

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'cond({self.relation_id[:20]})',
            action='EXIT',
            executed=True,
            reasoning=f'A={pa:.4f} back in [{lower:.4f}, {upper:.4f}] (was {prev})',
            signal_values={
                'price_a': pa,
                'price_b': float(self._price_b or 0),
                'lower': lower,
                'upper': upper,
            },
        )
        logger.info('EXIT conditional arb: A back in band')
