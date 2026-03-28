"""Shared mixin for relation-based arbitrage strategies."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from coinjure.market.relations import MarketRelation, RelationStore
from coinjure.ticker import Ticker
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide

logger = logging.getLogger(__name__)


class RelationArbMixin:
    """Provides common relation loading, token watching, and ticker matching."""

    _relation: MarketRelation | None
    _ids: list[str]
    _tokens: list[str]
    # Pre-computed sets: _match_sets[i] contains all identifiers that match slot i
    _match_sets: list[set[str]]
    # Tracks ticker symbols owned by this strategy instance
    _owned_symbols: set[str]

    def _init_from_relation(self, relation_id: str) -> None:
        """Load relation from store and extract market/token IDs."""
        self._relation = None
        self._ids = []
        self._tokens = []
        self._match_sets = []
        self._owned_symbols = set()
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)
            if self._relation and self._relation.status == 'invalidated':
                raise ValueError(
                    f'Relation {relation_id} is invalidated — validate it first'
                )

        if self._relation:
            for m in self._relation.markets:
                mid = (
                    m.get('condition_id', '')
                    or m.get('market_ticker', '')
                    or m.get('ticker', '')
                    or m.get('id', '')
                )
                self._ids.append(mid)
                tid = m.get('token_id', '')
                if not tid:
                    token_ids = m.get('token_ids', [])
                    tid = token_ids[0] if token_ids else ''
                self._tokens.append(tid)
                self._match_sets.append({mid, tid} - {''})

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        return [t for t in self._tokens if t]

    def _slot_matches(self, ticker: Ticker, slot: int) -> bool:
        """Check if a ticker's identifier matches a relation slot."""
        return ticker.identifier in self._match_sets[slot]

    def _find_ticker(
        self,
        trader: Trader,
        market_id: str,
        side: str = 'yes',
    ) -> Ticker | None:
        """Find a ticker in the data manager by market ID and side."""
        return trader.market_data.find_ticker_by_market(market_id, side)

    async def _place_pair(
        self,
        trader: Trader,
        ticker_1: Ticker | None,
        price_1: Decimal,
        ticker_2: Ticker | None,
        price_2: Decimal,
        quantity: Decimal,
    ) -> bool:
        """Place a two-leg order pair atomically (both or none).

        Returns True if both legs executed successfully.
        """
        if not ticker_1 or not ticker_2 or price_1 <= 0 or price_2 <= 0:
            logger.warning('Pair trade skipped: missing ticker or invalid price')
            return False

        r1 = await trader.place_order(
            side=TradeSide.BUY, ticker=ticker_1,
            limit_price=price_1, quantity=quantity,
        )
        if r1.failure_reason:
            logger.warning('Pair leg1 rejected: %s', r1.failure_reason)
            return False

        r2 = await trader.place_order(
            side=TradeSide.BUY, ticker=ticker_2,
            limit_price=price_2, quantity=quantity,
        )
        if r2.failure_reason:
            # Leg1 succeeded but leg2 failed — unwind leg1
            logger.warning('Pair leg2 rejected: %s, unwinding leg1', r2.failure_reason)
            unwound = False
            best_bid = trader.market_data.get_best_bid(ticker_1)
            if best_bid:
                try:
                    r_unwind = await asyncio.wait_for(
                        trader.place_order(
                            side=TradeSide.SELL, ticker=ticker_1,
                            limit_price=best_bid.price, quantity=quantity,
                        ),
                        timeout=5.0,
                    )
                    unwound = not r_unwind.failure_reason
                except asyncio.TimeoutError:
                    logger.critical(
                        'CRITICAL: Pair unwind timed out after 5s for %s — '
                        'orphaned position requires manual intervention',
                        ticker_1.symbol,
                    )
            if not unwound:
                # Unwind failed — track position so _close_owned can clean up later
                logger.warning('Pair unwind failed, tracking orphaned leg1: %s', ticker_1.symbol)
                self._owned_symbols.add(ticker_1.symbol)
            return False

        self._owned_symbols.add(ticker_1.symbol)
        self._owned_symbols.add(ticker_2.symbol)
        return True

    async def _close_owned(self, trader: Trader) -> None:
        """Close only positions opened by this strategy."""
        closed: list[str] = []
        for sym in list(self._owned_symbols):
            pos = trader.position_manager.positions.get(sym)
            if pos is None or pos.quantity <= 0:
                closed.append(sym)
                continue
            best_bid = trader.market_data.get_best_bid(pos.ticker)
            if best_bid:
                result = await trader.place_order(
                    side=TradeSide.SELL, ticker=pos.ticker,
                    limit_price=best_bid.price, quantity=pos.quantity,
                )
                if not result.failure_reason:
                    closed.append(sym)
        for sym in closed:
            self._owned_symbols.discard(sym)
