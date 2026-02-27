import time
from decimal import Decimal
from typing import Any

from coinjure.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide


class PolymarketPaperStrategy(Strategy):
    """Conservative paper-trading strategy for yes/no markets.

    Buys when implied probability is low, sells when it is high,
    and enforces cooldown + max position per ticker.
    """

    def __init__(
        self,
        trade_size: str = '5',
        buy_below: str = '0.43',
        sell_above: str = '0.57',
        max_spread: str = '0.03',
        max_position_per_ticker: str = '30',
        cooldown_seconds: float = 45.0,
        allowed_tickers: str = '',
        debug_events: int = 0,
        buy_limit_cap: str = '0.99',
        sell_limit_floor: str = '0.01',
        cross_amount: str = '0.50',
    ) -> None:
        self.trade_size = Decimal(str(trade_size))
        self.buy_below = Decimal(str(buy_below))
        self.sell_above = Decimal(str(sell_above))
        self.max_spread = Decimal(str(max_spread))
        self.max_position_per_ticker = Decimal(str(max_position_per_ticker))
        self.cooldown_seconds = float(cooldown_seconds)
        self.positions: dict[str, Decimal] = {}
        self.last_trade_ts: dict[str, float] = {}
        self.allowed_tickers = {
            t.strip() for t in str(allowed_tickers).split(',') if t.strip()
        }
        self.debug_events = int(debug_events)
        self._debug_seen = 0
        self.buy_limit_cap = Decimal(str(buy_limit_cap))
        self.sell_limit_floor = Decimal(str(sell_limit_floor))
        self.cross_amount = Decimal(str(cross_amount))

    @staticmethod
    def _as_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _get_first_attr(event: Event, names: tuple[str, ...]) -> object | None:
        for name in names:
            if hasattr(event, name):
                value = getattr(event, name)
                if value is not None:
                    return value
        return None

    def _extract_ticker(self, event: Event) -> str | None:
        raw = self._get_first_attr(
            event,
            (
                'ticker',
                'symbol',
                'asset',
                'token',
                'token_id',
                'market',
                'market_id',
                'event_id',
            ),
        )
        return raw

    @staticmethod
    def _ticker_identifiers(ticker: Any, event: Event) -> set[str]:
        ids: set[str] = set()
        if ticker is not None:
            ids.add(str(ticker))
            for attr in ('symbol', 'id', 'token_id', 'market_id', 'event_id'):
                if hasattr(ticker, attr):
                    val = getattr(ticker, attr)
                    if val is not None:
                        ids.add(str(val))
        for attr in ('ticker', 'symbol', 'token_id', 'market_id', 'event_id'):
            if hasattr(event, attr):
                val = getattr(event, attr)
                if val is not None:
                    ids.add(str(val))
        return ids

    def _ticker_key(self, ticker: Any, event: Event) -> str:
        ids = self._ticker_identifiers(ticker, event)
        for preferred in ('symbol', 'token_id', 'market_id', 'event_id'):
            if hasattr(event, preferred):
                val = getattr(event, preferred)
                if val is not None:
                    return str(val)
        return sorted(ids)[0] if ids else 'UNKNOWN'

    def _extract_price(self, event: Event) -> Decimal | None:
        price = self._as_decimal(
            self._get_first_attr(
                event,
                ('price', 'yes_price', 'last_price', 'mark_price', 'mid_price', 'mid'),
            )
        )
        if price is None:
            return None
        if price <= Decimal('0') or price >= Decimal('1'):
            return None
        return price

    def _extract_bid_ask(self, event: Event) -> tuple[Decimal | None, Decimal | None]:
        bid = self._as_decimal(
            self._get_first_attr(event, ('best_bid', 'bid', 'yes_bid', 'top_bid'))
        )
        ask = self._as_decimal(
            self._get_first_attr(event, ('best_ask', 'ask', 'yes_ask', 'top_ask'))
        )
        if bid is not None and (bid <= Decimal('0') or bid >= Decimal('1')):
            bid = None
        if ask is not None and (ask <= Decimal('0') or ask >= Decimal('1')):
            ask = None
        return bid, ask

    def _on_cooldown(self, ticker: str, now_ts: float) -> bool:
        last_ts = self.last_trade_ts.get(ticker)
        if last_ts is None:
            return False
        return (now_ts - last_ts) < self.cooldown_seconds

    async def _place(
        self,
        trader: Trader,
        side: TradeSide,
        ticker: Any,
        ticker_key: str,
        limit_price: Decimal,
        quantity: Decimal,
    ) -> None:
        await trader.place_order(
            side=side,
            ticker=ticker,
            limit_price=limit_price,
            quantity=quantity,
        )
        signed = quantity if side == TradeSide.BUY else -quantity
        self.positions[ticker_key] = (
            self.positions.get(ticker_key, Decimal('0')) + signed
        )
        self.last_trade_ts[ticker_key] = time.time()

    def _limit_for_side(self, side: TradeSide, reference_price: Decimal) -> Decimal:
        if side == TradeSide.BUY:
            return min(self.buy_limit_cap, reference_price + self.cross_amount)
        return max(self.sell_limit_floor, reference_price - self.cross_amount)

    async def _maybe_trade_from_price(
        self, ticker: Any, ticker_key: str, price: Decimal, trader: Trader
    ) -> None:
        now_ts = time.time()
        if self._on_cooldown(ticker_key, now_ts):
            return
        pos = self.positions.get(ticker_key, Decimal('0'))

        if price <= self.buy_below and pos < self.max_position_per_ticker:
            qty = min(self.trade_size, self.max_position_per_ticker - pos)
            if qty > Decimal('0'):
                limit_price = self._limit_for_side(TradeSide.BUY, price)
                await self._place(
                    trader, TradeSide.BUY, ticker, ticker_key, limit_price, qty
                )
            return

        if price >= self.sell_above and pos > Decimal('0'):
            qty = min(self.trade_size, pos)
            if qty > Decimal('0'):
                limit_price = self._limit_for_side(TradeSide.SELL, price)
                await self._place(
                    trader, TradeSide.SELL, ticker, ticker_key, limit_price, qty
                )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self._debug_seen < self.debug_events:
            self._debug_seen += 1
            raw_ticker = getattr(event, 'ticker', None)
            print(
                f"[strategy-debug] event_type={type(event).__name__} "
                f"has_collateral={hasattr(raw_ticker, 'collateral')} "
                f"ticker_ids={sorted(self._ticker_identifiers(raw_ticker, event))} "
                f"attrs={getattr(event, '__dict__', {})}"
            )

        if isinstance(event, NewsEvent):
            return

        ticker = self._extract_ticker(event)
        if not ticker:
            return
        if not hasattr(ticker, 'collateral'):
            return
        ticker_key = self._ticker_key(ticker, event)
        if self.allowed_tickers:
            ids = self._ticker_identifiers(ticker, event)
            if not any(t in ids for t in self.allowed_tickers):
                return

        if not ticker_key:
            return

        if isinstance(event, PriceChangeEvent):
            price = self._extract_price(event)
            if price is not None:
                await self._maybe_trade_from_price(ticker, ticker_key, price, trader)
            return

        if isinstance(event, OrderBookEvent):
            bid, ask = self._extract_bid_ask(event)
            if bid is None or ask is None or ask < bid:
                return
            spread = ask - bid
            if spread > self.max_spread:
                return
            mid = (bid + ask) / Decimal('2')
            await self._maybe_trade_from_price(ticker, ticker_key, mid, trader)
            return
