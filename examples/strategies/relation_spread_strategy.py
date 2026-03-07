"""RelationSpreadStrategy — trade spread deviations between related markets.

Takes a relation_id from the RelationStore (discovered by ``market discover``).
The relation defines two markets (A, B) and their hedge ratio.

The strategy has two phases:
  1. **Warmup** (first ``warmup`` events): collect spread samples, compute
     mean and std from the data itself.  No trades are placed.
  2. **Trading**: enter when |spread - mean| > entry_mult * std,
     exit when |spread - mean| < exit_mult * std.

This makes the strategy self-calibrating — it works on any data window
without needing pre-computed parameters.

Usage:
    coinjure strategy backtest \
      --parquet data/polymarket_orderbook_2026-03-05T02.parquet \
      --parquet data/polymarket_orderbook_2026-03-05T03.parquet \
      --market-id <condition_id_A> --market-id <condition_id_B> \
      --strategy-ref examples/strategies/relation_spread_strategy.py:RelationSpreadStrategy \
      --strategy-kwargs-json '{"relation_id": "678876-691547", "trade_size": 10}'
"""

from __future__ import annotations

import logging
import math
from collections import deque
from decimal import Decimal

from coinjure.engine.trader.trader import Trader
from coinjure.engine.trader.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.market.relations import RelationStore
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class RelationSpreadStrategy(Strategy):
    """Trade spread deviations between two related markets."""

    name = 'relation_spread'
    version = '0.2.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        hedge_ratio: float | None = None,
        lead_lag: int | None = None,
        entry_mult: float = 2.0,
        exit_mult: float = 0.5,
        warmup: int = 200,
        max_position: float = 100.0,
        # Allow manual override (skip warmup)
        expected_spread: float | None = None,
        entry_threshold: float | None = None,
        exit_threshold: float | None = None,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.max_position = Decimal(str(max_position))
        self._entry_mult = entry_mult
        self._exit_mult = exit_mult
        self._warmup_size = warmup

        # Load relation from store (for market IDs and hedge_ratio)
        self._relation = None
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)

        # Hedge ratio: explicit > relation > default
        if hedge_ratio is not None:
            self._hedge_ratio = Decimal(str(hedge_ratio))
        elif self._relation and self._relation.hedge_ratio:
            self._hedge_ratio = Decimal(str(self._relation.hedge_ratio))
        else:
            self._hedge_ratio = Decimal('1.0')

        # Lead-lag: explicit > relation > 0 (no lag)
        if lead_lag is not None:
            self._lead_lag = lead_lag
        elif self._relation and self._relation.lead_lag:
            self._lead_lag = self._relation.lead_lag
        else:
            self._lead_lag = 0

        # Market identifiers
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

        # Manual overrides — if all three are set, skip warmup
        if (
            expected_spread is not None
            and entry_threshold is not None
            and exit_threshold is not None
        ):
            self._expected_spread = Decimal(str(expected_spread))
            self._entry_threshold = Decimal(str(entry_threshold))
            self._exit_threshold = Decimal(str(exit_threshold))
            self._calibrated = True
        else:
            self._expected_spread = Decimal('0')
            self._entry_threshold = Decimal('0')
            self._exit_threshold = Decimal('0')
            self._calibrated = False

        # Warmup buffer
        self._spread_buffer: deque[float] = deque(maxlen=warmup)

        # Track latest prices for both legs.
        # When lead_lag > 0 (A leads B), we buffer A's prices and compare
        # A[t - lead_lag] with B[t].  When lead_lag < 0 (B leads A), we
        # buffer B's prices and compare A[t] with B[t - |lead_lag|].
        self._price_a: Decimal | None = None
        self._price_b: Decimal | None = None
        lag_buf_size = max(abs(self._lead_lag) + 1, 1)
        self._price_a_buf: deque[Decimal] = deque(maxlen=lag_buf_size)
        self._price_b_buf: deque[Decimal] = deque(maxlen=lag_buf_size)

        # Position state: 'flat', 'long_spread', 'short_spread'
        self._position_state = 'flat'

        # Collect YES token IDs for priority watching (skip NO tokens
        # to avoid matching their complement prices as leg prices).
        self._watch_token_ids: list[str] = []
        if self._relation:
            for mkt in (self._relation.market_a, self._relation.market_b):
                tid = mkt.get('token_id', '')
                if tid:
                    self._watch_token_ids.append(tid)

    def watch_tokens(self) -> list[str]:
        """Tell the engine which tokens to prioritize refreshing."""
        return self._watch_token_ids

    def _matches_market(self, ticker_id: str, market_id: str) -> bool:
        if not market_id:
            return False
        return market_id in ticker_id or ticker_id in market_id

    def _calibrate(self) -> None:
        """Compute expected_spread and thresholds from warmup samples."""
        n = len(self._spread_buffer)
        if n < 2:
            return
        mean = sum(self._spread_buffer) / n
        variance = sum((x - mean) ** 2 for x in self._spread_buffer) / n
        std = math.sqrt(variance)
        if std < 1e-8:
            # No variance → no trading opportunity
            self._calibrated = True
            self._expected_spread = Decimal(str(mean))
            self._entry_threshold = Decimal('999')  # never enter
            self._exit_threshold = Decimal('0')
            logger.info('Warmup done: spread has zero variance, no trades possible')
            return

        self._expected_spread = Decimal(str(mean))
        self._entry_threshold = Decimal(str(std * self._entry_mult))
        self._exit_threshold = Decimal(str(std * self._exit_mult))
        self._calibrated = True
        logger.info(
            'Warmup done (%d samples): mean=%.6f std=%.6f entry=%.6f exit=%.6f',
            n,
            mean,
            std,
            float(self._entry_threshold),
            float(self._exit_threshold),
        )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker

        # Skip NO-side events
        if (
            ticker.symbol.endswith('_NO')
            or getattr(ticker, 'token_id', '').endswith('_NO')
            or ticker.name.startswith('NO ')
        ):
            return

        tid = (
            getattr(ticker, 'market_id', '')
            or getattr(ticker, 'token_id', '')
            or getattr(ticker, 'market_ticker', '')
            or ticker.symbol
        )

        # Update price for the matching leg
        if self._matches_market(tid, self._id_a):
            self._price_a = event.price
            self._price_a_buf.append(event.price)
        elif self._matches_market(tid, self._id_b):
            self._price_b = event.price
            self._price_b_buf.append(event.price)
        else:
            return

        # Resolve time-shifted prices for spread computation.
        # lead_lag > 0: A leads B → use A[t - lag] vs B[t]
        # lead_lag < 0: B leads A → use A[t] vs B[t - |lag|]
        # lead_lag == 0: use A[t] vs B[t]
        if self._lead_lag > 0:
            if len(self._price_a_buf) < abs(self._lead_lag) + 1:
                return  # not enough A history yet
            price_a_shifted = self._price_a_buf[-(self._lead_lag + 1)]
            price_b_shifted = self._price_b
        elif self._lead_lag < 0:
            if len(self._price_b_buf) < abs(self._lead_lag) + 1:
                return  # not enough B history yet
            price_a_shifted = self._price_a
            price_b_shifted = self._price_b_buf[-(abs(self._lead_lag) + 1)]
        else:
            price_a_shifted = self._price_a
            price_b_shifted = self._price_b

        if price_a_shifted is None or price_b_shifted is None:
            return

        actual_spread = price_a_shifted - self._hedge_ratio * price_b_shifted
        spread_f = float(actual_spread)

        # ── Warmup phase ──
        if not self._calibrated:
            self._spread_buffer.append(spread_f)
            if len(self._spread_buffer) >= self._warmup_size:
                self._calibrate()
            return

        # ── Trading phase ──
        # Keep updating the rolling buffer for adaptive recalibration
        self._spread_buffer.append(spread_f)

        deviation = actual_spread - self._expected_spread

        if self._position_state == 'flat':
            if deviation > self._entry_threshold:
                await self._enter_short_spread(trader, deviation)
            elif deviation < -self._entry_threshold:
                await self._enter_long_spread(trader, deviation)
            else:
                self.record_decision(
                    ticker_name=f'spread({self.relation_id[:20]})',
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

    # ── Order helpers ──

    def _best_ask_price(self, trader: Trader, ticker) -> Decimal | None:
        best = trader.market_data.get_best_ask(ticker)
        return best.price if best else None

    async def _place_buy(self, trader: Trader, ticker, fallback_price: Decimal) -> None:
        ask = self._best_ask_price(trader, ticker)
        price = ask if ask else fallback_price + Decimal('0.05')
        await trader.place_order(
            side=TradeSide.BUY,
            ticker=ticker,
            limit_price=price,
            quantity=self.trade_size,
        )

    # ── Entry / Exit ──

    async def _enter_long_spread(self, trader: Trader, deviation: Decimal) -> None:
        """Buy A, sell B — deviation is negative (B overpriced)."""
        ticker_a = self._find_ticker(trader, self._id_a, yes=True)
        ticker_b_no = self._find_ticker(trader, self._id_b, yes=False)

        if ticker_a and self._price_a:
            await self._place_buy(trader, ticker_a, self._price_a)
        if ticker_b_no:
            await self._place_buy(
                trader,
                ticker_b_no,
                Decimal('1') - (self._price_b or Decimal('0.5')),
            )

        self._position_state = 'long_spread'
        self.record_decision(
            ticker_name=f'spread({self.relation_id[:20]})',
            action='BUY_SPREAD',
            executed=True,
            reasoning=(
                f'B overpriced: buy A @ {self._price_a}, sell B @ {self._price_b}, '
                f'dev={float(deviation):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info(
            'ENTER long_spread: buy A=%s sell B=%s dev=%.4f',
            self._price_a,
            self._price_b,
            deviation,
        )

    async def _enter_short_spread(self, trader: Trader, deviation: Decimal) -> None:
        """Sell A, buy B — deviation is positive (A overpriced)."""
        ticker_a_no = self._find_ticker(trader, self._id_a, yes=False)
        ticker_b = self._find_ticker(trader, self._id_b, yes=True)

        if ticker_a_no and self._price_a:
            await self._place_buy(
                trader,
                ticker_a_no,
                Decimal('1') - self._price_a,
            )
        if ticker_b and self._price_b:
            await self._place_buy(trader, ticker_b, self._price_b)

        self._position_state = 'short_spread'
        self.record_decision(
            ticker_name=f'spread({self.relation_id[:20]})',
            action='SELL_SPREAD',
            executed=True,
            reasoning=(
                f'A overpriced: sell A @ {self._price_a}, buy B @ {self._price_b}, '
                f'dev={float(deviation):.4f}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info(
            'ENTER short_spread: sell A=%s buy B=%s dev=%.4f',
            self._price_a,
            self._price_b,
            deviation,
        )

    async def _exit_position(self, trader: Trader, deviation: Decimal) -> None:
        """Close both legs — spread has converged."""
        for pos in trader.position_manager.positions.values():
            if hasattr(pos.ticker, 'token_id') and pos.quantity > 0:
                best_bid = trader.market_data.get_best_bid(pos.ticker)
                if best_bid:
                    await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=pos.ticker,
                        limit_price=best_bid.price,
                        quantity=pos.quantity,
                    )

        prev_state = self._position_state
        self._position_state = 'flat'
        self.record_decision(
            ticker_name=f'spread({self.relation_id[:20]})',
            action='CLOSE_SPREAD',
            executed=True,
            reasoning=(
                f'converged: was {prev_state}, dev={float(deviation):.4f} '
                f'< exit={float(self._exit_threshold)}'
            ),
            signal_values={
                'price_a': float(self._price_a or 0),
                'price_b': float(self._price_b or 0),
                'deviation': float(deviation),
            },
        )
        logger.info('EXIT %s: spread converged, dev=%.4f', prev_state, deviation)

    def _find_ticker(self, trader: Trader, market_id: str, yes: bool = True):
        """Find a YES or NO side ticker in the engine's order books."""
        for ticker in trader.market_data.order_books:
            is_no = (
                ticker.symbol.endswith('_NO')
                or getattr(ticker, 'token_id', '').endswith('_NO')
                or ticker.name.startswith('NO ')
            )
            if yes and is_no:
                continue
            if not yes and not is_no:
                continue
            tid = (
                getattr(ticker, 'market_id', '')
                or getattr(ticker, 'token_id', '')
                or getattr(ticker, 'market_ticker', '')
                or ticker.symbol
            )
            if self._matches_market(tid, market_id):
                return ticker
        return None
