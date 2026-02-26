from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from coinjure.events.events import Event
from coinjure.trader.trader import Trader


@dataclass
class StrategyDecision:
    """Generic decision record emitted by any strategy."""

    timestamp: str  # HH:MM:SS
    ticker_name: str
    action: str  # BUY_YES / BUY_NO / HOLD / CLOSE_* etc.
    executed: bool
    reasoning: str = ''
    confidence: float = 0.0  # 0.0 if not applicable
    signal_values: dict[str, float] = field(default_factory=dict)
    # Examples:
    # LLM:           {'llm_prob': 0.72, 'market_price': 0.55, 'edge': 0.17}
    # OBI:           {'imbalance': 0.42, 'bid_vol': 1200.0, 'ask_vol': 450.0}
    # Momentum:      {'momentum': 0.031}
    # MeanReversion: {'z_score': -1.8, 'mean': 0.52, 'std': 0.04}
    # MarketMaking:  {'spread': 0.08, 'mid': 0.50}


class Strategy(ABC):
    def set_paused(self, paused: bool) -> None:
        """Set control-plane pause state for this strategy."""
        setattr(self, '_paused', paused)

    def is_paused(self) -> bool:
        """Return whether control-plane has paused decision-making."""
        return bool(getattr(self, '_paused', False))

    @abstractmethod
    async def process_event(self, event: Event, trader: Trader) -> None:
        """Process an event"""
        pass

    def get_decisions(self) -> list[StrategyDecision]:
        """Return recent strategy decisions. Override in subclasses."""
        return []

    def get_decision_stats(self) -> dict[str, int | float]:
        """Return running decision counters. Override in subclasses."""
        return {}
