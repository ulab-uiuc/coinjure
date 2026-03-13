"""ImplicationArbStrategy — trade constraint violations on implication pairs.

For implication relations (A implies B), the constraint is A ≤ B.
Example: "Trump wins nomination" implies "Trump wins election", so
P(nomination) ≤ P(election) must always hold.

When the market violates this (price_A > price_B), we:
  - Sell A (buy A's NO token)
  - Buy B (buy B's YES token)
and exit when the constraint is restored (price_A ≤ price_B).

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/implication_arb_strategy.py:ImplicationArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "610381-677358"}'
"""

from __future__ import annotations

import logging
from decimal import Decimal

from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.relation_mixin import RelationArbMixin
from coinjure.strategy.strategy import Strategy
from coinjure.trading.sizing import compute_trade_size_with_llm
from coinjure.trading.trader import Trader

logger = logging.getLogger(__name__)


class ImplicationArbStrategy(RelationArbMixin, Strategy):
    """Arbitrage constraint violations on implication pairs (A ≤ B).

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be an implication type.
    trade_size:
        Max dollar amount per leg.
    min_edge:
        Minimum violation size (price_A - price_B) to trigger entry.
    kelly_fraction:
        Conservative Kelly multiplier for dynamic sizing.
    """

    name = 'implication_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        min_edge: float = 0.01,
        kelly_fraction: float = 0.1,
        llm_trade_sizing: bool = False,
        llm_model: str | None = None,
        llm_portfolio_review: bool = False,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.max_trade_size = Decimal(str(trade_size))
        self.min_edge = Decimal(str(min_edge))
        self.kelly_fraction = Decimal(str(kelly_fraction))
        self.llm_trade_sizing = llm_trade_sizing
        self.llm_model = llm_model
        self.llm_portfolio_review = llm_portfolio_review

        self._init_from_relation(relation_id)

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        self._position_state = 'flat'  # flat | short_a_long_b

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

        violation = self._price_a - self._price_b  # > 0 means constraint broken

        if self._position_state == 'flat':
            if violation > self.min_edge:
                await self._enter(trader, violation)
            else:
                self.record_decision(
                    ticker_name=f'impl({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={float(self._price_a):.4f} B={float(self._price_b):.4f} '
                        f'violation={float(violation):.4f} < min_edge={float(self.min_edge):.4f}'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'violation': float(violation),
                    },
                )
        else:
            # Exit when constraint restored
            if violation <= Decimal('0'):
                await self._exit(trader, violation)

    async def _enter(self, trader: Trader, violation: Decimal) -> None:
        """Sell A (buy NO), buy B (buy YES)."""
        size = await compute_trade_size_with_llm(
            trader.position_manager,
            violation,
            strategy_id=self.relation_id or self.name,
            strategy_type=self.name,
            relation_type='implication',
            llm_trade_sizing=self.llm_trade_sizing,
            llm_model=self.llm_model,
            kelly_fraction=self.kelly_fraction,
            max_size=self.max_trade_size,
            leg_count=2,
            leg_prices=[Decimal('1') - self._price_a, self._price_b],
        )
        ticker_a_no = self._find_ticker(trader, self._ids[0], side='no')
        ticker_b = self._find_ticker(trader, self._ids[1], side='yes')

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
            ticker_name=f'impl({self.relation_id[:20]})',
            action='ENTER_ARB',
            executed=True,
            reasoning=(
                f'Constraint violated: A={float(self._price_a):.4f} > '
                f'B={float(self._price_b):.4f}, violation={float(violation):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info(
            'ENTER implication arb: sell A=%s buy B=%s violation=%.4f',
            self._price_a,
            self._price_b,
            violation,
        )

    async def _exit(self, trader: Trader, violation: Decimal) -> None:
        """Close owned positions — constraint restored."""
        await self._close_owned(trader)

        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'impl({self.relation_id[:20]})',
            action='EXIT_ARB',
            executed=True,
            reasoning=(
                f'Constraint restored: A={float(self._price_a):.4f} ≤ '
                f'B={float(self._price_b):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info('EXIT implication arb: constraint restored')
