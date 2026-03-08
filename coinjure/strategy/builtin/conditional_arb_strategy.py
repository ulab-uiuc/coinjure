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

from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.relation_mixin import RelationArbMixin
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class ConditionalArbStrategy(RelationArbMixin, Strategy):
    """Arbitrage conditional probability constraint violations.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore.
    trade_size:
        Dollar amount per leg.
    cond_lower:
        Lower bound on p(A|B). Default 0 (no lower bound).
    cond_upper:
        Upper bound on p(A|B). Default 1 (no upper bound).
    min_edge:
        Minimum distance outside the band to trigger entry.
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
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.cond_lower = cond_lower
        self.cond_upper = cond_upper
        self.min_edge = Decimal(str(min_edge))

        self._init_from_relation(relation_id)

        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        # flat | long_a_short_b (A too low) | short_a_long_b (A too high)
        self._position_state = 'flat'

    def _compute_bounds(self, price_b: float) -> tuple[float, float]:
        """Compute the valid range for p(A) given p(B) and conditional bounds."""
        # p(A) = p(A|B)*p(B) + p(A|¬B)*(1-p(B))
        # Lower bound: p(A|B) = cond_lower, p(A|¬B) = 0
        lower = self.cond_lower * price_b
        # Upper bound: p(A|B) = cond_upper, p(A|¬B) = 1
        upper = self.cond_upper * price_b + (1 - price_b)
        return lower, upper

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

        pa = float(self._price_a)
        pb = float(self._price_b)
        lower, upper = self._compute_bounds(pb)

        if self._position_state == 'flat':
            if pa > upper + float(self.min_edge):
                # A too high → sell A, buy B
                await self._enter_short_a(trader, pa, lower, upper)
            elif pa < lower - float(self.min_edge):
                # A too low → buy A, sell B
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
                        'price_a': pa, 'price_b': pb,
                        'lower': lower, 'upper': upper,
                    },
                )
        else:
            # Exit when A is back inside the band
            if lower <= pa <= upper:
                await self._exit(trader, pa, lower, upper)

    async def _enter_short_a(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        """A too expensive → sell A (buy NO), buy B (buy YES)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b = self._find_ticker(trader, self._id_b, yes=True)

        if ticker_a_no and self._price_a:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_a_no,
                limit_price=Decimal('1') - self._price_a,
                quantity=self.trade_size,
            )
        if ticker_b and self._price_b:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b,
                limit_price=self._price_b, quantity=self.trade_size,
            )

        self._position_state = 'short_a_long_b'
        self.record_decision(
            ticker_name=f'cond({self.relation_id[:20]})',
            action='ENTER_SHORT_A',
            executed=True,
            reasoning=f'A={pa:.4f} > upper={upper:.4f}: sell A, buy B',
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
            },
        )
        logger.info('ENTER conditional arb: A too high, sell A buy B')

    async def _enter_long_a(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        """A too cheap → buy A (buy YES), sell B (buy NO)."""
        ticker_a = self._find_ticker(trader, self._id_a, yes=True)
        ticker_b_no = self._find_ticker(trader, self._id_b, yes=False)

        if ticker_a and self._price_a:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_a,
                limit_price=self._price_a, quantity=self.trade_size,
            )
        if ticker_b_no and self._price_b:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b_no,
                limit_price=Decimal('1') - self._price_b,
                quantity=self.trade_size,
            )

        self._position_state = 'long_a_short_b'
        self.record_decision(
            ticker_name=f'cond({self.relation_id[:20]})',
            action='ENTER_LONG_A',
            executed=True,
            reasoning=f'A={pa:.4f} < lower={lower:.4f}: buy A, sell B',
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
            },
        )
        logger.info('ENTER conditional arb: A too low, buy A sell B')

    async def _exit(
        self, trader: Trader, pa: float, lower: float, upper: float,
    ) -> None:
        for pos in trader.position_manager.positions.values():
            if pos.quantity > 0:
                best_bid = trader.market_data.get_best_bid(pos.ticker)
                if best_bid:
                    await trader.place_order(
                        side=TradeSide.SELL, ticker=pos.ticker,
                        limit_price=best_bid.price, quantity=pos.quantity,
                    )

        prev = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'cond({self.relation_id[:20]})',
            action='EXIT',
            executed=True,
            reasoning=f'A={pa:.4f} back in [{lower:.4f}, {upper:.4f}] (was {prev})',
            signal_values={
                'price_a': pa, 'price_b': float(self._price_b or 0),
                'lower': lower, 'upper': upper,
            },
        )
        logger.info('EXIT conditional arb: A back in band')

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
