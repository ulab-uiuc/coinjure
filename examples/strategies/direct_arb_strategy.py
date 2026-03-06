"""DirectArbStrategy — CLI-constructible cross-platform arbitrage.

Unlike CrossPlatformArbStrategy (which requires a pre-built MarketMatcher),
this strategy accepts plain string IDs in its constructor so it can be
instantiated directly from CLI kwargs:

    coinjure paper run \\
        --exchange cross_platform \\
        --strategy-ref examples/strategies/direct_arb_strategy.py:DirectArbStrategy \\
        --strategy-kwargs-json '{
            "poly_market_id": "0xabc...",
            "poly_token_id":  "1234...",
            "kalshi_ticker":  "KXNBA-25-LAL"
        }'

The strategy matches incoming events by:
  - PolyMarketTicker.market_id == poly_market_id  (YES side)
  - KalshiTicker.market_ticker == kalshi_ticker    (YES side)

When it has a fresh price for both sides it checks for arb and, if the
net edge exceeds min_edge, places both legs simultaneously.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from coinjure.engine.execution.trader import Trader
from coinjure.engine.execution.types import TradeSide
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import KalshiTicker, PolyMarketTicker

logger = logging.getLogger(__name__)

# Conservative fee estimate per side (round-trip)
_FEE_PER_SIDE = Decimal('0.005')


class DirectArbStrategy(Strategy):
    """Cross-platform arb strategy targeting a single pre-specified market pair.

    All constructor args are JSON-serialisable so the strategy can be
    registered and promoted via ``portfolio add`` + ``portfolio promote``
    without writing any code.

    Parameters
    ----------
    poly_market_id:
        Polymarket market ID (the ``id`` field from Gamma API / arb scan output).
    poly_token_id:
        Polymarket YES-token CLOB ID (the ``poly_token_id`` from arb scan output).
        Used to construct NO-side orders; if empty, NO leg is skipped.
    kalshi_ticker:
        Kalshi market ticker (the ``kalshi_ticker`` from arb scan output).
    min_edge:
        Minimum gross price gap to trigger a trade (default 0.02 = 2%).
    trade_size:
        Dollar amount per leg (default 10).
    cooldown_seconds:
        Minimum seconds between arb attempts on this pair (default 60).
    """

    name = 'direct_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        poly_market_id: str,
        poly_token_id: str = '',
        kalshi_ticker: str = '',
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: int = 60,
    ) -> None:
        super().__init__()
        self.poly_market_id = poly_market_id
        self.poly_token_id = poly_token_id
        self.kalshi_ticker_str = kalshi_ticker
        self.min_edge = Decimal(str(min_edge))
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_seconds = cooldown_seconds

        # Latest prices (updated on every matching event)
        self._poly_yes_price: Decimal | None = None
        self._kalshi_yes_price: Decimal | None = None

        # Store actual ticker objects once we see them in the stream
        self._poly_ticker: PolyMarketTicker | None = None
        self._kalshi_ticker_obj: KalshiTicker | None = None

        self._last_arb_time: float = 0.0

    async def process_event(self, event: Event, trader: Trader) -> None:  # noqa: C901
        if self.is_paused():
            return

        ticker = getattr(event, 'ticker', None)
        if ticker is None:
            return

        price: Decimal | None = None

        if isinstance(event, PriceChangeEvent):
            price = event.price
        elif isinstance(event, OrderBookEvent):
            # Use the event price as a directional indicator;
            # bid events update our YES price (what we can sell at),
            # ask events update what we'd have to pay.
            # For a simple arb signal, treat any price update as indicative.
            price = event.price
        else:
            return

        if price is None or price <= 0:
            return

        # --- Match and store Polymarket price ---
        if isinstance(ticker, PolyMarketTicker):
            if ticker.market_id == self.poly_market_id:
                self._poly_yes_price = price
                if self._poly_ticker is None:
                    self._poly_ticker = ticker
                    logger.debug(
                        'DirectArb: matched poly ticker %s (market_id=%s)',
                        ticker.symbol[:20],
                        self.poly_market_id[:16],
                    )

        # --- Match and store Kalshi price ---
        elif isinstance(ticker, KalshiTicker):
            if (
                self.kalshi_ticker_str
                and ticker.market_ticker == self.kalshi_ticker_str
            ):
                self._kalshi_yes_price = price
                if self._kalshi_ticker_obj is None:
                    self._kalshi_ticker_obj = ticker
                    logger.debug(
                        'DirectArb: matched kalshi ticker %s',
                        ticker.symbol[:20],
                    )

        # --- Check arb if we have both prices ---
        if self._poly_yes_price is not None and self._kalshi_yes_price is not None:
            await self._check_arb(trader)

    async def _check_arb(self, trader: Trader) -> None:
        poly_yes = self._poly_yes_price  # type: ignore[assignment]
        kalshi_yes = self._kalshi_yes_price  # type: ignore[assignment]

        # Direction 1: Poly cheaper → buy Poly YES + buy Kalshi NO
        # profit = kalshi_yes - poly_yes (before fees)
        edge_poly_cheap = kalshi_yes - poly_yes

        # Direction 2: Kalshi cheaper → buy Kalshi YES + buy Poly NO
        # profit = poly_yes - kalshi_yes (before fees)
        edge_kalshi_cheap = poly_yes - kalshi_yes

        gross_edge = max(edge_poly_cheap, edge_kalshi_cheap)
        net_edge = gross_edge - _FEE_PER_SIDE * 2

        if net_edge < self.min_edge:
            self.record_decision(
                ticker_name=f'poly:{self.poly_market_id[:16]}',
                action='HOLD',
                executed=False,
                reasoning=(
                    f'net_edge={float(net_edge):.4f} < min_edge={float(self.min_edge):.4f} '
                    f'(poly={float(poly_yes):.4f} kalshi={float(kalshi_yes):.4f})'
                ),
                signal_values={
                    'poly_yes': float(poly_yes),
                    'kalshi_yes': float(kalshi_yes),
                    'gross_edge': float(gross_edge),
                    'net_edge': float(net_edge),
                },
            )
            return

        # Cooldown guard
        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        if edge_poly_cheap >= edge_kalshi_cheap:
            await self._trade_poly_cheap(trader, poly_yes, kalshi_yes, gross_edge)
        else:
            await self._trade_kalshi_cheap(trader, poly_yes, kalshi_yes, gross_edge)

    async def _trade_poly_cheap(
        self,
        trader: Trader,
        poly_yes: Decimal,
        kalshi_yes: Decimal,
        edge: Decimal,
    ) -> None:
        """Buy Poly YES + Buy Kalshi NO."""
        poly_ticker = self._poly_ticker
        kalshi_ticker = self._kalshi_ticker_obj
        if poly_ticker is None or kalshi_ticker is None:
            return

        label = f'poly:{self.poly_market_id[:16]}'
        executed = False

        # Leg 1: Buy Poly YES
        try:
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=poly_ticker,
                limit_price=poly_yes,
                quantity=self.trade_size,
            )
            if not result.failure_reason:
                executed = True
                logger.info('ARB leg1: buy Poly YES @ %s', poly_yes)
        except Exception:
            logger.exception('ARB leg1 failed (buy Poly YES)')

        # Leg 2: Buy Kalshi NO
        kalshi_no = kalshi_ticker.get_no_ticker()
        if kalshi_no is not None and executed:
            no_price = Decimal('1') - kalshi_yes
            try:
                result2 = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=kalshi_no,
                    limit_price=no_price,
                    quantity=self.trade_size,
                )
                if result2.failure_reason:
                    logger.warning('ARB leg2 failed: %s', result2.failure_reason)
            except Exception:
                logger.exception('ARB leg2 failed (buy Kalshi NO)')

        self.record_decision(
            ticker_name=label,
            action='ARB_BUY_POLY',
            executed=executed,
            reasoning=(
                f'Poly cheap: poly={float(poly_yes):.4f} kalshi={float(kalshi_yes):.4f} '
                f'edge={float(edge):.4f}'
            ),
            signal_values={
                'poly_yes': float(poly_yes),
                'kalshi_yes': float(kalshi_yes),
                'edge': float(edge),
                'direction': -1.0,
            },
        )

    async def _trade_kalshi_cheap(
        self,
        trader: Trader,
        poly_yes: Decimal,
        kalshi_yes: Decimal,
        edge: Decimal,
    ) -> None:
        """Buy Kalshi YES + Buy Poly NO."""
        poly_ticker = self._poly_ticker
        kalshi_ticker = self._kalshi_ticker_obj
        if poly_ticker is None or kalshi_ticker is None:
            return

        label = f'poly:{self.poly_market_id[:16]}'
        executed = False

        # Leg 1: Buy Kalshi YES
        try:
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=kalshi_ticker,
                limit_price=kalshi_yes,
                quantity=self.trade_size,
            )
            if not result.failure_reason:
                executed = True
                logger.info('ARB leg1: buy Kalshi YES @ %s', kalshi_yes)
        except Exception:
            logger.exception('ARB leg1 failed (buy Kalshi YES)')

        # Leg 2: Buy Poly NO
        poly_no = poly_ticker.get_no_ticker()
        if poly_no is not None and executed:
            no_price = Decimal('1') - poly_yes
            try:
                result2 = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=poly_no,
                    limit_price=no_price,
                    quantity=self.trade_size,
                )
                if result2.failure_reason:
                    logger.warning('ARB leg2 failed: %s', result2.failure_reason)
            except Exception:
                logger.exception('ARB leg2 failed (buy Poly NO)')

        self.record_decision(
            ticker_name=label,
            action='ARB_BUY_KALSHI',
            executed=executed,
            reasoning=(
                f'Kalshi cheap: kalshi={float(kalshi_yes):.4f} poly={float(poly_yes):.4f} '
                f'edge={float(edge):.4f}'
            ),
            signal_values={
                'poly_yes': float(poly_yes),
                'kalshi_yes': float(kalshi_yes),
                'edge': float(edge),
                'direction': 1.0,
            },
        )
