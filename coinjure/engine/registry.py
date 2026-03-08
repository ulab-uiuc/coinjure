"""Strategy Registry — persistent store for the multi-strategy portfolio."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_DIR = Path.home() / '.coinjure'
REGISTRY_PATH = REGISTRY_DIR / 'portfolio.json'

VALID_LIFECYCLES = frozenset(
    {'pending_backtest', 'paper_trading', 'deployed', 'live_trading', 'retired'}
)

_FIELDS = {
    'strategy_id',
    'strategy_ref',
    'strategy_kwargs',
    'lifecycle',
    'created_at',
    'exchange',
    'backtest_pnl',
    'paper_pnl',
    'last_signal_at',
    'pid',
    'socket_path',
    'data_dir',
    'relation_id',
    'retired_reason',
}


@dataclass
class StrategyEntry:
    """A single strategy instance tracked by the portfolio."""

    strategy_id: str
    strategy_ref: str
    strategy_kwargs: dict = field(default_factory=dict)
    lifecycle: str = 'pending_backtest'
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    exchange: str = 'polymarket'
    backtest_pnl: str | None = None
    paper_pnl: str | None = None
    last_signal_at: str | None = None
    pid: int | None = None
    socket_path: str | None = None
    data_dir: str = ''
    relation_id: str | None = None
    retired_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> StrategyEntry:
        return cls(**{k: v for k, v in d.items() if k in _FIELDS})


class StrategyRegistry:
    """JSON-file-based registry of all portfolio strategies.

    Supports concurrent reads; writes are atomic (tmp → rename).
    """

    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self.path = path
        self._entries: dict[str, StrategyEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            for entry_dict in data.get('strategies', []):
                try:
                    entry = StrategyEntry.from_dict(entry_dict)
                    self._entries[entry.strategy_id] = entry
                except Exception:
                    logger.warning('Skipping malformed registry entry: %s', entry_dict)
        except Exception:
            logger.warning('Failed to load registry from %s', self.path, exc_info=True)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'saved_at': datetime.now().isoformat(),
            'strategies': [e.to_dict() for e in self._entries.values()],
        }
        tmp = self.path.parent / (self.path.name + '.tmp')
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)
        except Exception:
            logger.exception('Failed to save registry to %s', self.path)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> list[StrategyEntry]:
        return list(self._entries.values())

    def get(self, strategy_id: str) -> StrategyEntry | None:
        return self._entries.get(strategy_id)

    def add(self, entry: StrategyEntry) -> None:
        if entry.strategy_id in self._entries:
            raise ValueError(f'Strategy already exists: {entry.strategy_id!r}')
        self._entries[entry.strategy_id] = entry
        self._save()

    def update(self, entry: StrategyEntry) -> None:
        self._entries[entry.strategy_id] = entry
        self._save()

    def remove(self, strategy_id: str) -> None:
        self._entries.pop(strategy_id, None)
        self._save()
