"""Persistent market relation graph — stores discovered spread groups.

Relation types (8 types → 7 strategies):
  - implication    : A implies B (A ≤ B), date/threshold nesting
  - exclusivity    : mutually exclusive outcomes (Σ ≤ 1) → GroupArbStrategy
  - complementary  : outcomes sum to 1 within an event (Σ ≈ 1) → GroupArbStrategy
  - same_event     : identical market across platforms (A ≈ B)
  - correlated     : statistically correlated prices (shared drivers)
  - structural     : known mathematical relationship (e.g. price nesting)
  - conditional    : conditional probability bounds
  - temporal       : lead-lag information flow
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)

# Alias to avoid shadowing the builtin `list` with RelationStore.list method
_List = list

RELATIONS_DIR = Path.home() / '.coinjure'
RELATIONS_PATH = RELATIONS_DIR / 'relations.json'

VALID_TYPES = frozenset(
    {
        'same_event',
        'complementary',
        'implication',
        'exclusivity',
        'correlated',
        'structural',
        'conditional',
        'temporal',
    }
)


@dataclass
class MarketRelation:
    """A discovered relationship between prediction markets."""

    relation_id: str
    markets: list[dict[str, Any]] = field(default_factory=list)
    spread_type: str = 'unknown'
    confidence: float = 0.0
    reasoning: str = ''

    # Quantitative hypothesis (set by analyze/discover)
    hypothesis: str = ''  # e.g. "p_A - p_B ≈ 0"
    hedge_ratio: float = 1.0  # β from OLS: p_A = α + β * p_B
    lead_lag: int = 0  # positive = A leads B by N steps

    # Lifecycle
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = (
        'active'  # active, backtest_passed, backtest_failed, deployed, retired
    )

    # Backtest results (set by engine backtest)
    backtest_pnl: float | None = None
    backtest_trades: int | None = None

    def set_backtest_result(
        self, passed: bool, pnl: float = 0.0, trades: int = 0
    ) -> None:
        """Update lifecycle based on backtest outcome."""
        self.status = 'backtest_passed' if passed else 'backtest_failed'
        self.backtest_pnl = pnl
        self.backtest_trades = trades

    def get_token_id(self, index: int = 0) -> str:
        """Get the first YES-side CLOB token_id for market at *index*.

        Checks token_ids (list), then token_id (singular), then falls back
        to the market dict's 'id' field.
        """
        if index < 0 or index >= len(self.markets):
            return ''
        m = self.markets[index]
        token_ids = m.get('token_ids', [])
        if token_ids:
            return str(token_ids[0])
        token_id = m.get('token_id', '')
        if token_id:
            return str(token_id)
        return str(m.get('id', ''))

    def get_no_token_id(self, index: int = 0) -> str:
        """Get the NO-side CLOB token_id for market at *index*.

        Checks token_ids[1] (list), then no_token_id (singular).
        Returns empty string if not available.
        """
        if index < 0 or index >= len(self.markets):
            return ''
        m = self.markets[index]
        token_ids = m.get('token_ids', [])
        if len(token_ids) >= 2:
            return str(token_ids[1])
        no_token_id = m.get('no_token_id', '')
        if no_token_id:
            return str(no_token_id)
        return ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MarketRelation:
        d = dict(d)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


class RelationStore:
    """JSON-backed store for market relations."""

    def __init__(self, path: Path = RELATIONS_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._path.with_suffix('.lock')

    @contextlib.contextmanager
    def _file_lock(self):
        """Acquire an exclusive file lock for the duration of the block.

        Falls back to a no-op on platforms where ``fcntl`` is unavailable
        (e.g. Windows).
        """
        if not _HAS_FCNTL:
            yield
            return
        lock_fd = open(self._lock_path, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, data: list[dict]) -> None:
        tmp = self._path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(self._path)

    def list(
        self, spread_type: str | None = None, status: str | None = None
    ) -> list[MarketRelation]:
        raw = self._load()
        relations = [MarketRelation.from_dict(d) for d in raw]
        if spread_type:
            relations = [r for r in relations if r.spread_type == spread_type]
        if status:
            relations = [r for r in relations if r.status == status]
        return relations

    def get(self, relation_id: str) -> MarketRelation | None:
        for d in self._load():
            if d.get('relation_id') == relation_id:
                return MarketRelation.from_dict(d)
        return None

    def add(self, relation: MarketRelation) -> None:
        with self._file_lock():
            data = self._load()
            # Deduplicate by relation_id
            data = [d for d in data if d.get('relation_id') != relation.relation_id]
            data.append(relation.to_dict())
            self._save(data)

    def update(self, relation: MarketRelation) -> None:
        with self._file_lock():
            data = self._load()
            for i, d in enumerate(data):
                if d.get('relation_id') == relation.relation_id:
                    data[i] = relation.to_dict()
                    self._save(data)
                    return
            # Not found — add
            data.append(relation.to_dict())
            self._save(data)

    def add_batch(self, relations: _List[MarketRelation]) -> int:
        """Add multiple relations at once (upsert semantics)."""
        if not relations:
            return 0
        with self._file_lock():
            data = self._load()
            existing_ids = {d.get('relation_id') for d in data}
            added = 0
            for rel in relations:
                if rel.relation_id in existing_ids:
                    data = [d for d in data if d.get('relation_id') != rel.relation_id]
                else:
                    added += 1
                data.append(rel.to_dict())
            self._save(data)
        return added

    def remove(self, relation_id: str) -> bool:
        with self._file_lock():
            data = self._load()
            before = len(data)
            data = [d for d in data if d.get('relation_id') != relation_id]
            if len(data) < before:
                self._save(data)
                return True
            return False

    # ── Graph queries ──────────────────────────────────────────────────

    def find_by_market(self, market_id: str) -> _List[MarketRelation]:
        """Return all relations involving a given market (by any id field)."""
        results = []
        for r in self.list():
            for m in r.markets:
                ids = {
                    m.get('market_id', ''),
                    m.get('id', ''),
                    m.get('ticker', ''),
                    *m.get('token_ids', []),
                }
                if market_id in ids:
                    results.append(r)
                    break
        return results
