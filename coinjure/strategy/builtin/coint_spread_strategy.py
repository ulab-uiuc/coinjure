"""CointSpreadStrategy — cointegration-based mean reversion spread trading.

For semantic/conditional relations where two markets are cointegrated,
the spread (A - hedge_ratio * B) is stationary and mean-reverting.

Entry: spread deviates beyond entry_mult × std from its mean.
Exit: spread reverts within exit_mult × std of its mean.

The strategy self-calibrates during a warmup phase by computing the
spread mean and standard deviation from live data.

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/coint_spread_strategy.py:CointSpreadStrategy \\
      --strategy-kwargs-json '{"relation_id": "610380-610379"}'
"""

from __future__ import annotations

import logging
import math
from collections import deque
from decimal import Decimal

from coinjure.engine.trader.trader import Trader
from coinjure.engine.trader.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.relation_mixin import RelationArbMixin
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class CointSpreadStrategy(RelationArbMixin, Strategy):
    """Cointegration-based mean reversion on stationary spreads.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Should be semantic or conditional type.
    trade_size:
        Dollar amount per leg.
    hedge_ratio:
        Override hedge ratio (if None, loaded from relation or defaults to 1.0).
    entry_mult:
        Entry at mean ± entry_mult × std (default 2.0).
    exit_mult:
        Exit at mean ± exit_mult × std (default 0.5).
    warmup:
        Number of spread samples before trading starts.
    max_position:
        Maximum position size per leg.
    """

    name = 'coint_spread'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        hedge_ratio: float | None = None,
        entry_mult: float = 2.0,
        exit_mult: float = 0.5,
        warmup: int = 200,
        max_position: float = 100.0,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.max_position = Decimal(str(max_position))
        self._entry_mult = entry_mult
        self._exit_mult = exit_mult
        self._warmup_size = warmup

        self._init_from_relation(relation_id)

        # Hedge ratio
        if hedge_ratio is not None:
            self._hedge_ratio = Decimal(str(hedge_ratio))
        elif self._relation and self._relation.hedge_ratio:
            self._hedge_ratio = Decimal(str(self._relation.hedge_ratio))
        else:
            self._hedge_ratio = Decimal('1.0')

        # Calibration state
        self._spread_buffer: deque[float] = deque(maxlen=warmup)
        self._calibrated = False
        self._expected_spread = Decimal('0')
        self._entry_threshold = Decimal('0')
        self._exit_threshold = Decimal('0')

        # Prices
        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None

        # Position: flat, long_spread, short_spread
        self._position_state = 'flat'

    def _calibrate(self) -> None:
        n = len(self._spread_buffer)
        if n < 2:
            return
        mean = sum(self._spread_buffer) / n
        variance = sum((x - mean) ** 2 for x in self._spread_buffer) / n
        std = math.sqrt(variance)
        if std < 1e-8:
            self._calibrated = True
            self._expected_spread = Decimal(str(mean))
            self._entry_threshold = Decimal('999')
            self._exit_threshold = Decimal('0')
            logger.info('Warmup: zero variance, no trades possible')
            return

        self._expected_spread = Decimal(str(mean))
        self._entry_threshold = Decimal(str(std * self._entry_mult))
        self._exit_threshold = Decimal(str(std * self._exit_mult))
        self._calibrated = True
        logger.info(
            'Warmup done (%d): mean=%.6f std=%.6f entry=%.6f exit=%.6f',
            n, mean, std, float(self._entry_threshold), float(self._exit_threshold),
        )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        if getattr(ticker, 'is_no_side', False):
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

        spread = self._price_a - self._hedge_ratio * self._price_b
        spread_f = float(spread)

        # Warmup
        if not self._calibrated:
            self._spread_buffer.append(spread_f)
            if len(self._spread_buffer) >= self._warmup_size:
                self._calibrate()
            return

        self._spread_buffer.append(spread_f)
        deviation = spread - self._expected_spread

        if self._position_state == 'flat':
            if deviation > self._entry_threshold:
                await self._enter_short_spread(trader, deviation)
            elif deviation < -self._entry_threshold:
                await self._enter_long_spread(trader, deviation)
            else:
                self.record_decision(
                    ticker_name=f'coint({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'spread={spread_f:.4f} dev={float(deviation):.4f} '
                        f'within [{-float(self._entry_threshold):.4f}, '
                        f'{float(self._entry_threshold):.4f}]'
                    ),
                    signal_values={
                        'price_a': float(self._price_a),
                        'price_b': float(self._price_b),
                        'spread': spread_f,
                        'deviation': float(deviation),
                    },
                )
        else:
            if abs(deviation) < self._exit_threshold:
                await self._exit_position(trader, deviation)

    async def _enter_long_spread(self, trader: Trader, deviation: Decimal) -> None:
        """Buy A, sell B — spread is below mean (B overpriced relative to A)."""
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

        self._position_state = 'long_spread'
        self.record_decision(
            ticker_name=f'coint({self.relation_id[:20]})',
            action='BUY_SPREAD',
            executed=True,
            reasoning=f'Spread below mean: dev={float(deviation):.4f}',
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info('ENTER long_spread: dev=%.4f', deviation)

    async def _enter_short_spread(self, trader: Trader, deviation: Decimal) -> None:
        """Sell A, buy B — spread is above mean (A overpriced relative to B)."""
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

        self._position_state = 'short_spread'
        self.record_decision(
            ticker_name=f'coint({self.relation_id[:20]})',
            action='SELL_SPREAD',
            executed=True,
            reasoning=f'Spread above mean: dev={float(deviation):.4f}',
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info('ENTER short_spread: dev=%.4f', deviation)

    async def _exit_position(self, trader: Trader, deviation: Decimal) -> None:
        """Close both legs — spread has converged."""
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
            ticker_name=f'coint({self.relation_id[:20]})',
            action='CLOSE_SPREAD',
            executed=True,
            reasoning=f'Spread converged: was {prev}, dev={float(deviation):.4f}',
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info('EXIT %s: spread converged, dev=%.4f', prev, deviation)

    def _find_ticker(self, trader: Trader, market_id: str, yes: bool = True):
        for ticker in trader.market_data.order_books:
            is_no = getattr(ticker, 'is_no_side', False)
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
