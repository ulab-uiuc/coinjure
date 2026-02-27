from __future__ import annotations

from collections import deque
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

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

    def record_decision(
        self,
        *,
        ticker_name: str,
        action: str,
        executed: bool,
        reasoning: str = '',
        confidence: float = 0.0,
        signal_values: dict[str, float] | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Record a strategy decision in a shared default buffer.

        Strategies can override storage by implementing custom `get_decisions`
        / `get_decision_stats`, but this helper keeps legacy strategies
        functional and provides a common baseline for agent-authored code.
        """
        decisions = self._decision_buffer()
        stats = self._decision_stats()

        signal_payload = signal_values or {}
        decisions.append(
            StrategyDecision(
                timestamp=timestamp or datetime.now().strftime('%H:%M:%S'),
                ticker_name=ticker_name[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                confidence=confidence,
                signal_values=signal_payload,
            )
        )

        stats['decisions'] += 1
        action_key = action.upper()
        if action_key == 'HOLD':
            stats['holds'] += 1
            return

        if not executed:
            return

        stats['executed'] += 1
        if action_key in {'BUY', 'BUY_YES'}:
            stats['buy_yes'] += 1
        elif action_key == 'BUY_NO':
            stats['buy_no'] += 1
        elif action_key.startswith('SELL'):
            stats['sells'] += 1
        elif action_key.startswith('CLOSE'):
            stats['closes'] += 1

    def get_decisions(self) -> list[StrategyDecision]:
        """Return recent strategy decisions. Override in subclasses."""
        return list(self._decision_buffer())

    def get_decision_stats(self) -> dict[str, int | float]:
        """Return running decision counters. Override in subclasses."""
        return dict(self._decision_stats())

    def _decision_buffer(self) -> deque[StrategyDecision]:
        buf = getattr(self, '_decisions', None)
        if isinstance(buf, deque):
            return buf
        fresh: deque[StrategyDecision] = deque(maxlen=200)
        setattr(self, '_decisions', fresh)
        return fresh

    def _decision_stats(self) -> dict[str, int]:
        stats = getattr(self, '_decision_stats_cache', None)
        if isinstance(stats, dict):
            return stats
        fresh = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'buy_no': 0,
            'sells': 0,
            'closes': 0,
            'holds': 0,
        }
        setattr(self, '_decision_stats_cache', fresh)
        return fresh
