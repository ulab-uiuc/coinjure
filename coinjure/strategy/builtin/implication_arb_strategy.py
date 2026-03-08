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

from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.relation_mixin import RelationArbMixin
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class ImplicationArbStrategy(RelationArbMixin, Strategy):
    """Arbitrage constraint violations on implication pairs (A ≤ B).

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be an implication type.
    trade_size:
        Dollar amount per leg.
    min_edge:
        Minimum violation size (price_A - price_B) to trigger entry.
    """

    name = 'implication_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        min_edge: float = 0.01,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.min_edge = Decimal(str(min_edge))

        self._init_from_relation(relation_id)

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        self._position_state = 'flat'  # flat | short_a_long_b

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        if getattr(ticker, 'side', 'yes') == 'no':
            return

        tid = (
            getattr(ticker, 'market_id', '')
            or getattr(ticker, 'token_id', '')
            or ticker.symbol
        )

        if self._matches(tid, self._id_a, self._token_a):
            self._price_a = event.price
        elif self._matches(tid, self._id_b, self._token_b):
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
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b = self._find_ticker(trader, self._id_b, yes=True)

        if ticker_a_no and self._price_a:
            no_price = Decimal('1') - self._price_a
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_a_no,
                limit_price=no_price, quantity=self.trade_size,
            )
        if ticker_b and self._price_b:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b,
                limit_price=self._price_b, quantity=self.trade_size,
            )

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
            self._price_a, self._price_b, violation,
        )

    async def _exit(self, trader: Trader, violation: Decimal) -> None:
        """Close all positions — constraint restored."""
        for pos in trader.position_manager.positions.values():
            if pos.quantity > 0:
                best_bid = trader.market_data.get_best_bid(pos.ticker)
                if best_bid:
                    await trader.place_order(
                        side=TradeSide.SELL, ticker=pos.ticker,
                        limit_price=best_bid.price, quantity=pos.quantity,
                    )

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

    def _find_ticker(self, trader: Trader, market_id: str, yes: bool = True):
        for ticker in trader.market_data.order_books:
            is_no = getattr(ticker, 'side', 'yes') == 'no'
            if yes and is_no:
                continue
            if not yes and not is_no:
                continue
            tid = (
                getattr(ticker, 'market_id', '')
                or getattr(ticker, 'token_id', '')
                or ticker.symbol
            )
            if self._matches(tid, market_id):
                return ticker
        return None
