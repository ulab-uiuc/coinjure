"""Shared mixin for relation-based arbitrage strategies."""

from __future__ import annotations

from coinjure.market.relations import RelationStore


class RelationArbMixin:
    """Provides common relation loading, token watching, and ticker matching."""

    _relation: object | None
    _id_a: str
    _id_b: str
    _token_a: str
    _token_b: str

    def _init_from_relation(self, relation_id: str) -> None:
        """Load relation from store and extract market/token IDs."""
        self._relation = None
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)
            if self._relation and self._relation.status == 'invalidated':
                raise ValueError(
                    f'Relation {relation_id} is invalidated — validate it first'
                )

        if self._relation:
            self._id_a = self._relation.market_a.get(
                'condition_id', ''
            ) or self._relation.market_a.get('id', '')
            self._id_b = self._relation.market_b.get(
                'condition_id', ''
            ) or self._relation.market_b.get('id', '')
            self._token_a = self._relation.market_a.get('token_id', '')
            self._token_b = self._relation.market_b.get('token_id', '')
        else:
            self._id_a = ''
            self._id_b = ''
            self._token_a = ''
            self._token_b = ''

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        tokens = []
        if self._token_a:
            tokens.append(self._token_a)
        if self._token_b:
            tokens.append(self._token_b)
        return tokens

    def _matches(self, ticker_id: str, market_id: str, token_id: str = '') -> bool:
        if market_id and (market_id in ticker_id or ticker_id in market_id):
            return True
        if token_id and ticker_id == token_id:
            return True
        return False
