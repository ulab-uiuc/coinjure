"""GroupArbStrategy — unified group constraint arbitrage.

For group relations (exclusivity, complementary) where N markets in an event
should satisfy sum(prices) ≈ 1.0 (or ≤ 1.0), this strategy:

    Case A — underpriced (sum < 1.0):
        Buy YES on every outcome.
        Cost   = sum(ask_YES_i)            < 1.0
        Payout = 1.0 (exactly one wins)
        Profit = 1.0 - sum(ask_YES_i)      > 0

    Case B — overpriced (sum > 1.0):
        Buy NO on every outcome.
        Cost   = sum(1 - ask_YES_i) = N - sum(ask_YES_i)
        Payout = N - 1 (all N-1 losing YES settle NO)
        Profit = sum(ask_YES_i) - 1.0      > 0

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/group_arb_strategy.py:GroupArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "m1-m2-m3"}'
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.market.relations import RelationStore
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import PolyMarketTicker
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide

logger = logging.getLogger(__name__)

_FEE_PER_SIDE = Decimal('0.005')


class GroupArbStrategy(Strategy):
    """Unified group constraint arbitrage for exclusivity/complementary relations.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be exclusivity or complementary.
    event_id:
        Polymarket event ID (alternative to relation_id for ad-hoc usage).
    min_edge:
        Minimum net profit per share after fees to trigger (default 0.02).
    trade_size:
        Dollar amount per leg.
    cooldown_seconds:
        Minimum seconds between arb executions (default 120).
    min_markets:
        Require at least this many markets before attempting arb (default 2).
    """

    name = 'group_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        event_id: str = '',
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: int = 120,
        min_markets: int = 2,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.min_edge = Decimal(str(min_edge))
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_seconds = cooldown_seconds
        self.min_markets = min_markets

        self._event_id = event_id
        self._relation_market_ids: set[str] = set()
        if relation_id and not event_id:
            store = RelationStore()
            rel = store.get(relation_id)
            if rel:
                for m in rel.markets:
                    eid = m.get('event_id', '')
                    if eid:
                        self._event_id = eid
                    mid = m.get('id', '')
                    if mid:
                        self._relation_market_ids.add(mid)

        self._asks: dict[str, Decimal] = {}
        self._tickers: dict[str, PolyMarketTicker] = {}
        self._last_arb_time: float = 0.0

    def watch_tokens(self) -> list[str]:
        tokens = []
        for ticker in self._tickers.values():
            tid = getattr(ticker, 'token_id', '')
            if tid:
                tokens.append(tid)
        return tokens

    def _should_track(self, ticker: PolyMarketTicker) -> bool:
        if self._event_id and ticker.event_id == self._event_id:
            return True
        if self._relation_market_ids and ticker.market_id in self._relation_market_ids:
            return True
        return False

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        ticker = getattr(event, 'ticker', None)
        if not isinstance(ticker, PolyMarketTicker):
            return
        if not self._should_track(ticker):
            return

        mid = ticker.market_id
        if not mid:
            return

        if mid not in self._tickers:
            self._tickers[mid] = ticker
            logger.debug(
                'GroupArb: registered market %s (%s)',
                mid[:16],
                ticker.name[:40] if ticker.name else '?',
            )

        if isinstance(event, OrderBookEvent):
            if event.side == 'ask' and event.price > 0:
                self._asks[mid] = event.price
            elif event.side == 'bid' and mid not in self._asks:
                self._asks[mid] = event.price
        elif isinstance(event, PriceChangeEvent):
            if mid not in self._asks:
                self._asks[mid] = event.price

        await self._check_arb(trader)

    async def _check_arb(self, trader: Trader) -> None:
        if len(self._asks) < self.min_markets:
            return

        market_ids = [mid for mid in self._asks if mid in self._tickers]
        if len(market_ids) < self.min_markets:
            return

        prices = {mid: self._asks[mid] for mid in market_ids}
        sum_yes = sum(prices.values())
        n = len(prices)

        edge_buy_yes = Decimal('1') - sum_yes - _FEE_PER_SIDE * n
        edge_buy_no = sum_yes - Decimal('1') - _FEE_PER_SIDE * n
        best_edge = max(edge_buy_yes, edge_buy_no)
        action = 'BUY_YES' if edge_buy_yes >= edge_buy_no else 'BUY_NO'

        signal = {
            'sum_yes': float(sum_yes),
            'n_markets': n,
            'edge_buy_yes': float(edge_buy_yes),
            'edge_buy_no': float(edge_buy_no),
            'best_edge': float(best_edge),
        }

        label = f'group({self.relation_id[:16] or self._event_id[:16]})'

        if best_edge < self.min_edge:
            self.record_decision(
                ticker_name=label,
                action='HOLD',
                executed=False,
                reasoning=(
                    f'sum_yes={float(sum_yes):.4f} n={n} '
                    f'best_edge={float(best_edge):.4f} < min={float(self.min_edge):.4f}'
                ),
                signal_values=signal,
            )
            return

        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        logger.info(
            'GroupArb: %s %s sum_yes=%.4f n=%d edge=%.4f',
            action,
            label,
            float(sum_yes),
            n,
            float(best_edge),
        )

        executed_legs = 0
        failed_legs = 0

        for mid, ask_price in prices.items():
            ticker = self._tickers[mid]

            if action == 'BUY_YES':
                trade_ticker = ticker
                leg_price = ask_price
            else:
                no_ticker = trader.market_data.find_complement(ticker)
                if no_ticker is None:
                    logger.warning(
                        'GroupArb: no NO ticker for market %s, skipping leg',
                        mid[:16],
                    )
                    continue
                trade_ticker = no_ticker
                leg_price = Decimal('1') - ask_price

            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=trade_ticker,
                    limit_price=leg_price,
                    quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning('GroupArb leg failed: %s', result.failure_reason)
                    failed_legs += 1
                else:
                    executed_legs += 1
            except Exception:
                logger.exception(
                    'GroupArb: exception placing leg for market %s', mid[:16]
                )
                failed_legs += 1

        self.record_decision(
            ticker_name=label,
            action=action,
            executed=executed_legs > 0,
            reasoning=(
                f'sum_yes={float(sum_yes):.4f} n={n} edge={float(best_edge):.4f} '
                f'legs={executed_legs}/{n} failed={failed_legs}'
            ),
            signal_values={
                **signal,
                'executed_legs': executed_legs,
                'failed_legs': failed_legs,
            },
        )
