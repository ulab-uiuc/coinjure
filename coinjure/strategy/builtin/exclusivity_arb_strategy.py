"""ExclusivityArbStrategy — trade constraint violations on exclusive pairs.

For exclusivity relations (A and B mutually exclusive), the constraint is
A + B ≤ 1. Example: "AOC wins Dem nomination" and "Ossoff wins Dem nomination"
can't both happen, so P(A) + P(B) ≤ 1.

When the market violates this (price_A + price_B > 1), we:
  - Sell A (buy A's NO token)
  - Sell B (buy B's NO token)
  Cost = (1 - price_A) + (1 - price_B) = 2 - (A + B) < 1
  Payout = at least 1 (at most one resolves YES, so at least one NO pays 1)
  Profit = payout - cost > 0

Exit when the constraint is restored (A + B ≤ 1).

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/exclusivity_arb_strategy.py:ExclusivityArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "559653-559661"}'
"""

from __future__ import annotations

import logging
from decimal import Decimal

from coinjure.engine.trader.trader import Trader
from coinjure.engine.trader.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.market.relations import RelationStore
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class ExclusivityArbStrategy(Strategy):
    """Arbitrage constraint violations on exclusive pairs (A + B ≤ 1).

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be an exclusivity type.
    trade_size:
        Dollar amount per leg.
    min_edge:
        Minimum violation size (A + B - 1) to trigger entry.
    """

    name = 'exclusivity_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        min_edge: float = 0.02,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.min_edge = Decimal(str(min_edge))

        self._relation = None
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)

        if self._relation:
            self._id_a = self._relation.market_a.get(
                'condition_id', ''
            ) or self._relation.market_a.get('id', '')
            self._id_b = self._relation.market_b.get(
                'condition_id', ''
            ) or self._relation.market_b.get('id', '')
        else:
            self._id_a = ''
            self._id_b = ''

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        self._position_state = 'flat'  # flat | short_both

    def _matches(self, ticker_id: str, market_id: str) -> bool:
        if not market_id:
            return False
        return market_id in ticker_id or ticker_id in market_id

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

        if self._matches(tid, self._id_a):
            self._price_a = event.price
        elif self._matches(tid, self._id_b):
            self._price_b = event.price
        else:
            return

        if self._price_a is None or self._price_b is None:
            return

        total = self._price_a + self._price_b
        violation = total - Decimal('1')  # > 0 means constraint broken

        if self._position_state == 'flat':
            if violation > self.min_edge:
                await self._enter(trader, violation)
            else:
                self.record_decision(
                    ticker_name=f'excl({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'A={float(self._price_a):.4f} B={float(self._price_b):.4f} '
                        f'sum={float(total):.4f} violation={float(violation):.4f}'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'sum': float(total),
                        'violation': float(violation),
                    },
                )
        else:
            if violation <= Decimal('0'):
                await self._exit(trader, violation)

    async def _enter(self, trader: Trader, violation: Decimal) -> None:
        """Sell both A and B (buy both NO tokens)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b_no = self._find_ticker(trader, self._id_b, yes=False)

        if ticker_a_no and self._price_a:
            no_price_a = Decimal('1') - self._price_a
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_a_no,
                limit_price=no_price_a, quantity=self.trade_size,
            )
        if ticker_b_no and self._price_b:
            no_price_b = Decimal('1') - self._price_b
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b_no,
                limit_price=no_price_b, quantity=self.trade_size,
            )

        self._position_state = 'short_both'
        self.record_decision(
            ticker_name=f'excl({self.relation_id[:20]})',
            action='ENTER_ARB',
            executed=True,
            reasoning=(
                f'Constraint violated: A={float(self._price_a):.4f} + '
                f'B={float(self._price_b):.4f} = {float(self._price_a + self._price_b):.4f} > 1'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info(
            'ENTER exclusivity arb: sell A=%s sell B=%s violation=%.4f',
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
            ticker_name=f'excl({self.relation_id[:20]})',
            action='EXIT_ARB',
            executed=True,
            reasoning=(
                f'Constraint restored: A={float(self._price_a):.4f} + '
                f'B={float(self._price_b):.4f} ≤ 1'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'violation': float(violation),
            },
        )
        logger.info('EXIT exclusivity arb: constraint restored')

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
