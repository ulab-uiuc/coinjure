"""Shared mixin for relation-based arbitrage strategies."""

from __future__ import annotations

from coinjure.market.relations import RelationStore
from coinjure.ticker import Ticker
from coinjure.trading.trader import Trader


class RelationArbMixin:
    """Provides common relation loading, token watching, and ticker matching."""

    _relation: object | None
    _ids: list[str]
    _tokens: list[str]
    # Pre-computed sets: _match_sets[i] contains all identifiers that match slot i
    _match_sets: list[set[str]]

    def _init_from_relation(self, relation_id: str) -> None:
        """Load relation from store and extract market/token IDs."""
        self._relation = None
        self._ids = []
        self._tokens = []
        self._match_sets = []
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
