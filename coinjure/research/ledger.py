"""Experiment & feedback ledgers — persistent JSONL stores under ~/.coinjure/."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEDGER_DIR = Path.home() / '.coinjure'
EXPERIMENT_LEDGER_PATH = LEDGER_DIR / 'experiment_ledger.jsonl'
FEEDBACK_LEDGER_PATH = LEDGER_DIR / 'feedback_ledger.jsonl'


# ---------------------------------------------------------------------------
# Experiment Ledger
# ---------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    """One experiment result (backtest / alpha-pipeline run)."""

    run_id: str
    timestamp: str
    strategy_ref: str
    strategy_kwargs: dict = field(default_factory=dict)
    market_id: str = ''
    event_id: str = ''
    history_file: str = ''
    gate_passed: bool = False
    metrics: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    notes: str = ''
    artifacts_dir: str = ''

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> LedgerEntry:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class ExperimentLedger:
    """Append-only JSONL ledger of experiment results."""

    def __init__(self, path: Path = EXPERIMENT_LEDGER_PATH) -> None:
        self.path = path

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: LedgerEntry) -> None:
        self._ensure_dir()
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry.to_dict()) + '\n')

    def load_all(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        entries: list[LedgerEntry] = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(LedgerEntry.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001
                logger.warning('Skipping malformed ledger line: %s', line[:120])
        return entries

    def query(
        self,
        *,
        tag: str | None = None,
        strategy_ref: str | None = None,
        market_id: str | None = None,
        gate_passed: bool | None = None,
    ) -> list[LedgerEntry]:
        results = self.load_all()
        if tag is not None:
            results = [e for e in results if tag in e.tags]
        if strategy_ref is not None:
            results = [e for e in results if strategy_ref in e.strategy_ref]
        if market_id is not None:
            results = [e for e in results if e.market_id == market_id]
        if gate_passed is not None:
            results = [e for e in results if e.gate_passed == gate_passed]
        return results

    def best(
        self,
        metric_key: str = 'total_pnl',
        top_n: int = 5,
    ) -> list[LedgerEntry]:
        entries = self.load_all()

        def _sort_key(e: LedgerEntry) -> float:
            val = e.metrics.get(metric_key)
            if val is None:
                return float('-inf')
            try:
                return float(val)
            except (TypeError, ValueError):
                return float('-inf')

        entries.sort(key=_sort_key, reverse=True)
        return entries[:top_n]

    def summary(self) -> dict[str, Any]:
        entries = self.load_all()
        if not entries:
            return {'total_experiments': 0}
        passed = [e for e in entries if e.gate_passed]
        strategy_refs = {e.strategy_ref for e in entries}
        market_ids = {e.market_id for e in entries if e.market_id}
        pnls = []
        for e in entries:
            try:
                pnls.append(float(e.metrics.get('total_pnl', 0)))
            except (TypeError, ValueError):
                pass
        return {
            'total_experiments': len(entries),
            'gate_pass_count': len(passed),
            'gate_pass_rate': round(len(passed) / len(entries), 3) if entries else 0,
            'unique_strategies': len(strategy_refs),
            'unique_markets': len(market_ids),
            'mean_pnl': round(sum(pnls) / len(pnls), 4) if pnls else None,
            'best_pnl': round(max(pnls), 4) if pnls else None,
        }


# ---------------------------------------------------------------------------
# Feedback Ledger
# ---------------------------------------------------------------------------


@dataclass
class FeedbackEntry:
    """Paper/live performance snapshot for comparison with backtest."""

    strategy_id: str
    timestamp: str
    source: str = 'paper'  # 'paper' or 'live'
    runtime_seconds: float = 0.0
    metrics: dict = field(default_factory=dict)
    decision_stats: dict = field(default_factory=dict)
    notes: str = ''

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FeedbackEntry:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class FeedbackLedger:
    """Append-only JSONL ledger of paper/live performance snapshots."""

    def __init__(self, path: Path = FEEDBACK_LEDGER_PATH) -> None:
        self.path = path

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: FeedbackEntry) -> None:
        self._ensure_dir()
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry.to_dict()) + '\n')

    def load_all(self) -> list[FeedbackEntry]:
        if not self.path.exists():
            return []
        entries: list[FeedbackEntry] = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(FeedbackEntry.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001
                logger.warning('Skipping malformed feedback line: %s', line[:120])
        return entries

    def for_strategy(self, strategy_id: str) -> list[FeedbackEntry]:
        return [e for e in self.load_all() if e.strategy_id == strategy_id]

    def latest(self, strategy_id: str) -> FeedbackEntry | None:
        entries = self.for_strategy(strategy_id)
        return entries[-1] if entries else None
