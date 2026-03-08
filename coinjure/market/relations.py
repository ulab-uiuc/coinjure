"""Persistent market relation graph — stores discovered spread pairs.

Relation types (by mathematical constraint):
  - same_event     : identical market across platforms (A ≈ B)
  - complementary  : outcomes sum to 1 within an event (Σ ≈ 1)
  - implication    : A implies B (A ≤ B)
  - exclusivity    : A and B mutually exclusive (A + B ≤ 1)
  - correlated     : statistically correlated prices (no structural constraint)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
class ValidationResult:
    """Quantitative validation result for a market relation."""

    # Analysis type that produced this result
    analysis_type: str | None = None  # 'structural', 'cointegration', 'lead_lag'

    # Structural analysis (same_event, complementary, implication, exclusivity)
    constraint: str | None = None  # e.g. 'A <= B', 'A + B <= 1'
    constraint_holds: bool | None = None
    violation_count: int | None = None  # number of times constraint violated
    violation_rate: float | None = None  # fraction of observations violating
    current_arb: float | None = None  # current constraint violation size
    mean_arb: float | None = None  # mean violation size when violated

    # Stationarity
    adf_statistic: float | None = None
    adf_pvalue: float | None = None
    is_stationary: bool | None = None

    # Cointegration
    coint_statistic: float | None = None
    coint_pvalue: float | None = None
    is_cointegrated: bool | None = None

    # Spread characteristics
    half_life: float | None = None  # bars to mean-revert
    hedge_ratio: float | None = None  # beta from OLS
    correlation: float | None = None
    mean_spread: float | None = None
    std_spread: float | None = None

    # Lead-lag
    lead_lag: int | None = None  # positive = A leads B by N steps
    lead_lag_corr: float | None = None  # cross-correlation at optimal lag
    lead_lag_significant: bool | None = None  # |corr| > threshold

    validated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_valid(self) -> bool:
        """Check validity based on the analysis type.

        For structural relations (implication, exclusivity, etc.), the logical
        relationship is always valid — constraint violations are trading
        opportunities, not evidence that the relation is wrong.
        """
        if self.analysis_type == 'structural':
            return True
        if self.analysis_type == 'lead_lag':
            return self.lead_lag_significant is True
        if self.is_cointegrated is not None:
            return self.is_cointegrated
        if self.is_stationary is not None:
            return self.is_stationary
        return False


@dataclass
class MarketRelation:
    """A discovered relationship between two prediction markets."""

    relation_id: str
    market_a: dict[str, Any] = field(default_factory=dict)
    market_b: dict[str, Any] = field(default_factory=dict)
    spread_type: str = 'unknown'
    confidence: float = 0.0
    reasoning: str = ''

    # Quantitative hypothesis (set by analyze/discover)
    hypothesis: str = ''  # e.g. "p_A - p_B ≈ 0"
    hedge_ratio: float = 1.0  # β from OLS: p_A = α + β * p_B
    lead_lag: int = 0  # positive = A leads B by N steps

    # Analysis results (set by discover/auto-pair)
    analysis_a: dict[str, Any] = field(default_factory=dict)
    analysis_b: dict[str, Any] = field(default_factory=dict)

    # Quantitative validation (set by validate command)
    validation: dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_validated: str | None = None
    valid_until: str | None = None
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

    def set_validation(self, result: ValidationResult) -> None:
        """Store a validation result and update trading fields."""
        self.validation = asdict(result)
        self.last_validated = result.validated_at
        # Propagate hedge ratio (relatively stable across windows)
        if result.hedge_ratio is not None:
            self.hedge_ratio = result.hedge_ratio
        if result.lead_lag is not None:
            self.lead_lag = result.lead_lag

    def get_validation(self) -> ValidationResult | None:
        if not self.validation:
            return None
        known = {f.name for f in ValidationResult.__dataclass_fields__.values()}
        return ValidationResult(
            **{k: v for k, v in self.validation.items() if k in known}
        )

    def get_token_id(self, leg: str = 'a') -> str:
        """Get the first YES-side CLOB token_id for leg 'a' or 'b'.

        Checks token_ids (list), then token_id (singular), then falls back
        to the market dict's 'id' field.
        """
        m = self.market_a if leg == 'a' else self.market_b
        token_ids = m.get('token_ids', [])
        if token_ids:
            return str(token_ids[0])
        token_id = m.get('token_id', '')
        if token_id:
            return str(token_id)
        return str(m.get('id', ''))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MarketRelation:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


class RelationStore:
    """JSON-backed store for market relations."""

    def __init__(self, path: Path = RELATIONS_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

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
        data = self._load()
        # Deduplicate by relation_id
        data = [d for d in data if d.get('relation_id') != relation.relation_id]
        data.append(relation.to_dict())
        self._save(data)

    def add_batch(self, relations: list[MarketRelation]) -> int:
        """Add multiple relations in a single load/save cycle. Returns count added."""
        if not relations:
            return 0
        data = self._load()
        existing_ids = {d.get('relation_id') for d in data}
        added = 0
        for rel in relations:
            if rel.relation_id in existing_ids:
                # Replace existing (upsert, consistent with add())
                data = [d for d in data if d.get('relation_id') != rel.relation_id]
            else:
                added += 1
            data.append(rel.to_dict())
        self._save(data)
        return added

    def update(self, relation: MarketRelation) -> None:
        data = self._load()
        for i, d in enumerate(data):
            if d.get('relation_id') == relation.relation_id:
                data[i] = relation.to_dict()
                self._save(data)
                return
        # Not found — add
        data.append(relation.to_dict())
        self._save(data)

    def add_batch(self, relations: list[MarketRelation]) -> int:
        """Add multiple relations at once (upsert semantics)."""
        if not relations:
            return 0
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
        data = self._load()
        before = len(data)
        data = [d for d in data if d.get('relation_id') != relation_id]
        if len(data) < before:
            self._save(data)
            return True
        return False

    # ── Graph queries ──────────────────────────────────────────────────

    def find_by_market(self, market_id: str) -> list[MarketRelation]:
        """Return all relations involving a given market (by any id field)."""
        results = []
        for r in self.list():
            a_ids = {
                r.market_a.get('market_id', ''),
                r.market_a.get('id', ''),
                r.market_a.get('ticker', ''),
                *r.market_a.get('token_ids', []),
            }
            b_ids = {
                r.market_b.get('market_id', ''),
                r.market_b.get('id', ''),
                r.market_b.get('ticker', ''),
                *r.market_b.get('token_ids', []),
            }
            if market_id in a_ids or market_id in b_ids:
                results.append(r)
        return results

    def strongest(self, n: int = 10, status: str | None = None) -> list[MarketRelation]:
        """Return the N highest-confidence relations."""
        relations = self.list(status=status)
        relations.sort(key=lambda r: r.confidence, reverse=True)
        return relations[:n]

    def validated(self) -> list[MarketRelation]:
        """Return relations that passed quantitative validation."""
        return self.list(status='validated')

    def invalidate(self, relation_id: str, reason: str = '') -> bool:
        """Mark a relation as invalidated."""
        r = self.get(relation_id)
        if r is None:
            return False
        r.status = 'invalidated'
        if reason:
            r.reasoning = f'{r.reasoning} [invalidated: {reason}]'
        self.update(r)
        return True

    def retire(self, relation_id: str) -> bool:
        """Mark a relation as retired (end of lifecycle)."""
        r = self.get(relation_id)
        if r is None:
            return False
        r.status = 'retired'
        self.update(r)
        return True
