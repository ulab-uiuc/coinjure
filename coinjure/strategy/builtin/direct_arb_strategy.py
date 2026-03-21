"""DirectArbStrategy — CLI-constructible cross-platform arbitrage.

Unlike CrossPlatformArbStrategy (which requires a pre-built MarketMatcher),
this strategy accepts plain string IDs in its constructor so it can be
instantiated directly from CLI kwargs:

    coinjure paper run \\
        --exchange cross_platform \\
        --strategy-ref coinjure/strategy/builtin/direct_arb_strategy.py:DirectArbStrategy \\
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

import asyncio
import logging
import time
from decimal import Decimal

from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import KalshiTicker, PolyMarketTicker
from coinjure.trading.sizing import compute_trade_size_with_llm
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide

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
        close_edge: float = 0.005,
        trade_size: float = 100.0,
        kelly_fraction: float = 0.1,
        cooldown_seconds: int = 60,
        warmup_seconds: float = 5.0,
        backtest_mode: bool = False,
        llm_trade_sizing: bool = False,
        llm_model: str | None = None,
        llm_portfolio_review: bool = False,
    ) -> None:
        super().__init__(warmup_seconds=warmup_seconds)
        self.poly_market_id = poly_market_id
        self.poly_token_id = poly_token_id
        self.kalshi_ticker_str = kalshi_ticker
        self.min_edge = Decimal(str(min_edge))
        self.close_edge = Decimal(str(close_edge))  # close when edge drops below this
        self.max_trade_size = Decimal(str(trade_size))
        self.kelly_fraction = Decimal(str(kelly_fraction))
        self.cooldown_seconds = cooldown_seconds
        self.backtest_mode = backtest_mode
        self.llm_trade_sizing = llm_trade_sizing
        self.llm_model = llm_model
        self.llm_portfolio_review = llm_portfolio_review

        # Latest mid prices (updated on every matching event)
        self._poly_yes_price: Decimal | None = None
        self._kalshi_yes_price: Decimal | None = None

        # Actual bid/ask prices from REST (used as limit_price when placing orders)
        self._poly_ask_price: Decimal | None = None
        self._poly_bid_price: Decimal | None = None
        self._kalshi_ask_price: Decimal | None = None
        self._kalshi_bid_price: Decimal | None = None

        # Store actual ticker objects once we see them in the stream
        self._poly_ticker: PolyMarketTicker | None = None
        self._kalshi_ticker_obj: KalshiTicker | None = None

        self._last_arb_time: float = float('-inf')  # no previous arb; first trade always allowed
        self._poll_task: asyncio.Task | None = None
        self._initialized: bool = False
        # Position tracking: 'poly_cheap' or 'kalshi_cheap' or None
        self._held_direction: str | None = None

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        tokens = []
        if self.poly_token_id:
            tokens.append(self.poly_token_id)
        return tokens

    async def _ensure_initialized(self, trader: Trader) -> None:
        """On first event, seed prices from REST and start background poll loop."""
        if self._initialized:
            return
        self._initialized = True
        if self.backtest_mode:
            return  # Historical data drives prices; skip REST fetch + poll
        await self._refresh_prices(trader)
        self._poll_task = asyncio.create_task(self._poll_loop(trader))

    async def _poll_loop(self, trader: Trader) -> None:
        """Refresh prices every 60s so stale cache markets get picked up."""
        while True:
            await asyncio.sleep(60)
            await self._refresh_prices(trader)

    async def _refresh_prices(self, trader: Trader) -> None:
        """Fetch current prices directly from Gamma + Kalshi REST APIs."""

        async def _fetch_poly() -> None:
            try:
                from coinjure.data.fetcher import polymarket_market_info

                info = await polymarket_market_info(self.poly_market_id)
                if info:
                    bid_str = info.get('best_bid', '')
                    ask_str = info.get('best_ask', '')
                    try:
                        b = float(bid_str) if bid_str else 0.0
                        a = float(ask_str) if ask_str else 0.0
                        mid = (b + a) / 2 if (b or a) else None
                    except (ValueError, TypeError):
                        mid = None
                    if mid is not None and mid > 0:
                        self._poly_yes_price = Decimal(str(mid))
                        if b > 0:
                            self._poly_bid_price = Decimal(str(b))
                        if a > 0:
                            self._poly_ask_price = Decimal(str(a))
                        if self._poly_ticker is None:
                            token_ids = info.get('token_ids', [])
                            self._poly_ticker = PolyMarketTicker(
                                symbol=token_ids[0]
                                if token_ids
                                else self.poly_token_id,
                                name=info.get('question', '')[:40],
                                token_id=token_ids[0]
                                if token_ids
                                else self.poly_token_id,
                                market_id=self.poly_market_id,
                                event_id=str(info.get('event_id', '')),
                            )
                        logger.debug(
                            'DirectArb: poly REST price %s (bid=%s ask=%s)',
                            self.poly_market_id,
                            b,
                            a,
                        )
            except Exception as exc:
                logger.debug('DirectArb: poly REST fetch failed: %s', exc)

        async def _fetch_kalshi() -> None:
            try:
                import httpx

                from coinjure.data.live.kalshi import KALSHI_API_URL

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f'{KALSHI_API_URL}/markets/{self.kalshi_ticker_str}'
                    )
                if resp.status_code == 200:
                    m = resp.json().get('market', resp.json())
                    yes_bid = m.get('yes_bid', 0) or 0
                    yes_ask = m.get('yes_ask', 0) or 0
                    if yes_bid or yes_ask:
                        mid = (yes_bid + yes_ask) / 2 / 100
                        self._kalshi_yes_price = Decimal(str(mid))
                        if yes_bid > 0:
                            self._kalshi_bid_price = Decimal(str(yes_bid)) / Decimal(
                                '100'
                            )
                        if yes_ask > 0:
                            self._kalshi_ask_price = Decimal(str(yes_ask)) / Decimal(
                                '100'
                            )
                        if self._kalshi_ticker_obj is None:
                            self._kalshi_ticker_obj = KalshiTicker(
                                symbol=self.kalshi_ticker_str,
                                name=m.get('title', '')[:40],
                                market_ticker=self.kalshi_ticker_str,
                                event_ticker=m.get('event_ticker', ''),
                                series_ticker='',
                            )
                        logger.debug(
                            'DirectArb: kalshi REST price %s (bid=%s ask=%s)',
                            self.kalshi_ticker_str,
                            yes_bid,
                            yes_ask,
                        )
            except Exception as exc:
                logger.debug('DirectArb: kalshi REST fetch failed: %s', exc)

        await asyncio.gather(_fetch_poly(), _fetch_kalshi())

        # Inject actual bid/ask into DataManager so PaperTrader can fill at real prices.
        # Use OrderBookEvent with actual bid/ask (not synthetic from PriceChangeEvent)
        # so a limit order at ask_price fills correctly.
        from coinjure.events import OrderBookEvent as _OBE

        _size = Decimal('1000')
        if self._poly_ticker is not None:
            if self._poly_ask_price is not None:
                trader.market_data.process_orderbook_event(
                    _OBE(
                        ticker=self._poly_ticker,
                        price=self._poly_ask_price,
                        size=_size,
                        size_delta=_size,
                        side='ask',
                    )
                )
            if self._poly_bid_price is not None:
                trader.market_data.process_orderbook_event(
                    _OBE(
                        ticker=self._poly_ticker,
                        price=self._poly_bid_price,
                        size=_size,
                        size_delta=_size,
                        side='bid',
                    )
                )
        if self._kalshi_ticker_obj is not None:
            if self._kalshi_ask_price is not None:
                trader.market_data.process_orderbook_event(
                    _OBE(
                        ticker=self._kalshi_ticker_obj,
                        price=self._kalshi_ask_price,
                        size=_size,
                        size_delta=_size,
                        side='ask',
                    )
                )
            if self._kalshi_bid_price is not None:
                trader.market_data.process_orderbook_event(
                    _OBE(
                        ticker=self._kalshi_ticker_obj,
                        price=self._kalshi_bid_price,
                        size=_size,
                        size_delta=_size,
                        side='bid',
                    )
                )

        if self._poly_yes_price is not None and self._kalshi_yes_price is not None:
            await self._check_arb(trader)

    async def process_event(self, event: Event, trader: Trader) -> None:  # noqa: C901
        await self._ensure_initialized(trader)
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
            # Match by market_id (from news/event stream) OR token_id (from watch_token CLOB stream)
            if ticker.market_id == self.poly_market_id or (
                self.poly_token_id and ticker.token_id == self.poly_token_id
            ):
                self._poly_yes_price = price
                if self._poly_ticker is None:
                    self._poly_ticker = ticker
                    logger.debug(
                        'DirectArb: matched poly ticker %s (market_id=%s token=%s)',
                        ticker.symbol[:20],
                        self.poly_market_id[:16],
                        ticker.token_id[:16] if ticker.token_id else '',
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
        poly_yes = self._poly_yes_price
        kalshi_yes = self._kalshi_yes_price
        if poly_yes is None or kalshi_yes is None:
            return

        # Direction 1: Poly cheaper → buy Poly YES + buy Kalshi NO
        edge_poly_cheap = kalshi_yes - poly_yes
        # Direction 2: Kalshi cheaper → buy Kalshi YES + buy Poly NO
        edge_kalshi_cheap = poly_yes - kalshi_yes

        gross_edge = max(edge_poly_cheap, edge_kalshi_cheap)
        net_edge = gross_edge - _FEE_PER_SIDE * 2

        label = f'poly:{self.poly_market_id[:16]}'
        signal = {
            'poly_yes': float(poly_yes),
            'kalshi_yes': float(kalshi_yes),
            'gross_edge': float(gross_edge),
            'net_edge': float(net_edge),
        }

        # ── Close logic: check if we should exit existing positions ──
        if self._held_direction is not None:
            # Edge in our held direction
            held_edge = (
                edge_poly_cheap
                if self._held_direction == 'poly_cheap'
                else edge_kalshi_cheap
            )
            held_net = held_edge - _FEE_PER_SIDE * 2

            if held_net < self.close_edge:
                # Edge gone or reversed — close positions
                await self._close_positions(
                    trader,
                    label,
                    signal,
                    f'edge_gone held_net={float(held_net):.4f}',
                )
                return

            # Still holding, don't add more
            return

        # ── Open logic: enter new position if edge is sufficient ──
        if net_edge < self.min_edge:
            return

        # Warmup & cooldown guard
        if self.is_warming_up():
            return
        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        size = await compute_trade_size_with_llm(
            trader.position_manager,
            net_edge,
            strategy_id=self.poly_market_id or self.kalshi_ticker_str or self.name,
            strategy_type=self.name,
            relation_type='same_event',
            llm_trade_sizing=self.llm_trade_sizing,
            llm_model=self.llm_model,
            kelly_fraction=self.kelly_fraction,
            max_size=self.max_trade_size,
            leg_count=2,
            leg_prices=[poly_yes, Decimal('1') - kalshi_yes],  # both non-None after guard above
        )

        if edge_poly_cheap >= edge_kalshi_cheap:
            executed = await self._trade_poly_cheap(
                trader, poly_yes, kalshi_yes, gross_edge, size
            )
            if executed:
                self._held_direction = 'poly_cheap'
        else:
            executed = await self._trade_kalshi_cheap(
                trader, poly_yes, kalshi_yes, gross_edge, size
            )
            if executed:
                self._held_direction = 'kalshi_cheap'

    async def _close_positions(
        self,
        trader: Trader,
        label: str,
        signal: dict,
        reason: str,
    ) -> None:
        """Sell all held positions to close the arb."""
        closed = 0
        direction = self._held_direction

        logger.info('DirectArb: CLOSE %s %s reason=%s', direction, label, reason)

        for pos in trader.position_manager.get_non_cash_positions():
            if pos.quantity <= 0:
                continue
            best_bid = trader.market_data.get_best_bid(pos.ticker)
            if best_bid is None or best_bid.price <= 0:
                continue
            try:
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=pos.ticker,
                    limit_price=best_bid.price,
                    quantity=pos.quantity,
                )
                if not result.failure_reason:
                    closed += 1
            except Exception:
                logger.exception(
                    'DirectArb: close failed for %s', pos.ticker.symbol[:20]
                )

        self.record_decision(
            ticker_name=label,
            action=f'CLOSE_{direction}',
            executed=closed > 0,
            reasoning=f'{reason} closed={closed}',
            signal_values=signal,
        )
        if closed > 0:
            self._held_direction = None
            self._last_arb_time = time.monotonic()

    async def _trade_poly_cheap(
        self,
        trader: Trader,
        poly_yes: Decimal,
        kalshi_yes: Decimal,
        edge: Decimal,
        size: Decimal,
    ) -> bool:
        """Buy Poly YES + Buy Kalshi NO. Returns True if leg1 executed."""
        poly_ticker = self._poly_ticker
        kalshi_ticker = self._kalshi_ticker_obj
        if poly_ticker is None or kalshi_ticker is None:
            return False

        label = f'poly:{self.poly_market_id[:16]}'
        executed = False

        # Leg 1: Buy Poly YES
        poly_limit = (
            self._poly_ask_price if self._poly_ask_price is not None else poly_yes
        )
        try:
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=poly_ticker,
                limit_price=poly_limit,
                quantity=size,
            )
            if not result.failure_reason:
                executed = True
                logger.info('ARB leg1: buy Poly YES @ %s qty=%s', poly_limit, size)
        except Exception:
            logger.exception('ARB leg1 failed (buy Poly YES)')

        # Leg 2: Buy Kalshi NO
        kalshi_no = trader.market_data.find_complement(kalshi_ticker)
        if kalshi_no is not None and executed:
            kalshi_bid = (
                self._kalshi_bid_price
                if self._kalshi_bid_price is not None
                else kalshi_yes
            )
            no_price = Decimal('1') - kalshi_bid
            try:
                result2 = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=kalshi_no,
                    limit_price=no_price,
                    quantity=size,
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
                f'OPEN poly_cheap: poly={float(poly_yes):.4f} kalshi={float(kalshi_yes):.4f} '
                f'edge={float(edge):.4f}'
            ),
            signal_values={
                'poly_yes': float(poly_yes),
                'kalshi_yes': float(kalshi_yes),
                'edge': float(edge),
                'direction': -1.0,
            },
        )
        return executed

    async def _trade_kalshi_cheap(
        self,
        trader: Trader,
        poly_yes: Decimal,
        kalshi_yes: Decimal,
        edge: Decimal,
        size: Decimal,
    ) -> bool:
        """Buy Kalshi YES + Buy Poly NO. Returns True if leg1 executed."""
        poly_ticker = self._poly_ticker
        kalshi_ticker = self._kalshi_ticker_obj
        if poly_ticker is None or kalshi_ticker is None:
            return False

        label = f'poly:{self.poly_market_id[:16]}'
        executed = False

        # Leg 1: Buy Kalshi YES
        kalshi_limit = (
            self._kalshi_ask_price if self._kalshi_ask_price is not None else kalshi_yes
        )
        try:
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=kalshi_ticker,
                limit_price=kalshi_limit,
                quantity=size,
            )
            if not result.failure_reason:
                executed = True
                logger.info('ARB leg1: buy Kalshi YES @ %s qty=%s', kalshi_limit, size)
        except Exception:
            logger.exception('ARB leg1 failed (buy Kalshi YES)')

        # Leg 2: Buy Poly NO
        poly_no = trader.market_data.find_complement(poly_ticker)
        if poly_no is not None and executed:
            poly_bid = (
                self._poly_bid_price if self._poly_bid_price is not None else poly_yes
            )
            no_price = Decimal('1') - poly_bid
            try:
                result2 = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=poly_no,
                    limit_price=no_price,
                    quantity=size,
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
                f'OPEN kalshi_cheap: kalshi={float(kalshi_yes):.4f} poly={float(poly_yes):.4f} '
                f'edge={float(edge):.4f}'
            ),
            signal_values={
                'poly_yes': float(poly_yes),
                'kalshi_yes': float(kalshi_yes),
                'edge': float(edge),
                'direction': 1.0,
            },
        )
        return executed
