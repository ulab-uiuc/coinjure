"""Shared mixin for relation-based arbitrage strategies."""

from __future__ import annotations

from coinjure.market.relations import RelationStore


class RelationArbMixin:
    """Provides common relation loading, token watching, and ticker matching."""

    _relation: object | None
    _ids: list[str]
    _tokens: list[str]

    def _init_from_relation(self, relation_id: str) -> None:
        """Load relation from store and extract market/token IDs."""
        self._relation = None
        self._ids = []
        self._tokens = []
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)
            if self._relation and self._relation.status == 'invalidated':
                raise ValueError(
                    f'Relation {relation_id} is invalidated — validate it first'
                )

        if self._relation:
            for m in self._relation.markets:
                self._ids.append(m.get('condition_id', '') or m.get('id', ''))
                # token_id (singular) for backward compat; token_ids[0] = YES token
                tid = m.get('token_id', '')
                if not tid:
                    token_ids = m.get('token_ids', [])
                    tid = token_ids[0] if token_ids else ''
                self._tokens.append(tid)

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        return [t for t in self._tokens if t]

    def _matches(self, ticker_id: str, market_id: str, token_id: str = '') -> bool:
        if market_id and (market_id in ticker_id or ticker_id in market_id):
            return True
        if token_id and ticker_id == token_id:
            return True
        return False
