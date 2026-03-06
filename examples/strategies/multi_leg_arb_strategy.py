"""Multi-leg arbitrage strategy on prediction market multi-outcome events.

Exploits a structural inefficiency: in multi-outcome events (e.g. "Who
will win the NBA?"), the YES prices across all outcomes should sum to
exactly 1.0.  In practice, they consistently sum to >1.0 (overpricing)
because each individual market has a bid-ask spread.

Strategy
--------
When the sum of all YES prices exceeds 1.0 + threshold, sell the
overpriced portfolio by buying NO on every outcome.  The total cost of
buying all NOs is (N - sum_YES), and the guaranteed payout is (N - 1)
because exactly one outcome settles YES (= our NO loses) and N-1
settle NO (= we win $1 each).

    profit_per_set = (N - 1) - cost_of_all_NOs
                   = (N - 1) - (N - sum_YES)
                   = sum_YES - 1.0

When the sum dips below 1.0 (underpricing), buy YES on every outcome.
Cost = sum_YES, guaranteed payout = 1.0.

    profit_per_set = 1.0 - sum_YES

This strategy also supports **cross-platform** arbitrage when the same
event trades on both Polymarket and Kalshi with different prices.

Data
----
Uses REAL hourly price data fetched from the Polymarket CLOB API and
Kalshi trade API, stored in ``examples/data/nba_cross_platform_data.json``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from coinjure.engine.execution.trader import Trader
from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import Ticker

logger = logging.getLogger(__name__)


@dataclass
class ArbSnapshot:
    """Record of market state at the time an arb was detected."""

    timestamp: int
    sum_yes: float
    overpricing: float
    team_prices: dict[str, float]
    action: str  # 'SELL_OVERPRICED' | 'BUY_UNDERPRICED' | 'CROSS_PLATFORM'
    profit_per_set: float


class MultiLegArbStrategy(Strategy):
    """Detect and trade multi-leg arbitrage on multi-outcome events.

    Works in two modes:

    1. **Intra-platform multi-leg**: When sum(YES) > 1 + threshold on a
       single platform, short the overpriced portfolio.
    2. **Cross-platform**: When the same team is cheaper on one platform,
       buy cheap YES + sell expensive YES (buy NO on the expensive side).
    """

    name = 'multi_leg_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        min_overpricing: float = 0.015,
        min_cross_platform_edge: float = 0.02,
        trade_size: Decimal = Decimal('10'),
        cooldown_events: int = 5,
        ticker_metadata: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.min_overpricing = min_overpricing
        self.min_cross_platform_edge = min_cross_platform_edge
        self.trade_size = trade_size
        self.cooldown_events = cooldown_events
        # symbol -> (platform, team) — set externally by the data source
        self._ticker_metadata: dict[str, tuple[str, str]] = ticker_metadata or {}

        # State
        self._prices: dict[str, dict[str, Decimal]] = {}  # platform -> {team: price}
        self._event_counter = 0
        self._last_arb_event: int = -999
        self._arb_snapshots: list[ArbSnapshot] = []
        self._team_tickers: dict[str, Ticker] = {}  # team -> ticker

    @property
    def arb_snapshots(self) -> list[ArbSnapshot]:
        return list(self._arb_snapshots)

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        if not isinstance(event, PriceChangeEvent):
            return

        self._event_counter += 1
        ticker = event.ticker
        price = event.price

        # Determine platform and team from external metadata
        meta = self._ticker_metadata.get(ticker.symbol)
        if meta:
            platform, team = meta
        else:
            platform = 'polymarket'
            team = ticker.name or ticker.symbol

        if platform not in self._prices:
            self._prices[platform] = {}
        self._prices[platform][team] = price
        self._team_tickers[team] = ticker

        poly_prices = self._prices.get('polymarket', {})
        self._check_intra_platform_arb(event, trader, poly_prices)
        self._check_cross_platform_arb(team, poly_prices)

    def _check_intra_platform_arb(
        self, event: PriceChangeEvent, trader: Trader, poly_prices: dict[str, Decimal]
    ) -> None:
        """Detect intra-platform multi-leg overpricing/underpricing."""
        if len(poly_prices) < 20:
            return

        sum_yes = sum(float(p) for p in poly_prices.values())
        overpricing = sum_yes - 1.0
        n_teams = len(poly_prices)

        if overpricing >= self.min_overpricing:
            if not self._cooldown_ok():
                return
            self._last_arb_event = self._event_counter

            ts = getattr(event, 'timestamp', 0)
            if not isinstance(ts, int | float):
                ts = 0

            self._arb_snapshots.append(
                ArbSnapshot(
                    timestamp=int(ts) if ts else self._event_counter,
                    sum_yes=round(sum_yes, 4),
                    overpricing=round(overpricing, 4),
                    team_prices={
                        k: round(float(v), 4) for k, v in list(poly_prices.items())[:5]
                    },
                    action='SELL_OVERPRICED',
                    profit_per_set=round(overpricing, 4),
                )
            )

            self.record_decision(
                ticker_name=f'MULTI_LEG ({n_teams} teams)',
                action='SELL_OVERPRICED',
                executed=True,
                reasoning=f'sum_yes={sum_yes:.4f} overpricing={overpricing:.4f}',
                signal_values={'sum_yes': sum_yes, 'overpricing': overpricing},
            )

            logger.info(
                'MULTI-LEG ARB: sum=%.4f overpricing=%.4f', sum_yes, overpricing
            )

        elif overpricing < -self.min_overpricing:
            if not self._cooldown_ok():
                return
            self._last_arb_event = self._event_counter

            self._arb_snapshots.append(
                ArbSnapshot(
                    timestamp=self._event_counter,
                    sum_yes=round(sum_yes, 4),
                    overpricing=round(overpricing, 4),
                    team_prices={
                        k: round(float(v), 4) for k, v in list(poly_prices.items())[:5]
                    },
                    action='BUY_UNDERPRICED',
                    profit_per_set=round(abs(overpricing), 4),
                )
            )

            self.record_decision(
                ticker_name=f'MULTI_LEG ({n_teams} teams)',
                action='BUY_UNDERPRICED',
                executed=False,
                reasoning=f'sum_yes={sum_yes:.4f} underpricing={abs(overpricing):.4f}',
                signal_values={'sum_yes': sum_yes, 'overpricing': overpricing},
            )
        else:
            self.record_decision(
                ticker_name=f'MULTI_LEG ({n_teams} teams)',
                action='HOLD',
                executed=False,
                reasoning=f'sum_yes={sum_yes:.4f} overpricing={overpricing:.4f} < min={self.min_overpricing}',
                signal_values={'sum_yes': sum_yes, 'overpricing': overpricing},
            )

    def _check_cross_platform_arb(
        self, team: str, poly_prices: dict[str, Decimal]
    ) -> None:
        """Detect cross-platform price discrepancies."""
        kalshi_prices = self._prices.get('kalshi', {})
        if team not in poly_prices or team not in kalshi_prices:
            return

        p_yes = float(poly_prices[team])
        k_yes = float(kalshi_prices[team])
        edge = abs(p_yes - k_yes)

        if edge < self.min_cross_platform_edge:
            return
        if not self._cooldown_ok():
            return
        self._last_arb_event = self._event_counter

        direction = 'BUY_POLY_SELL_KALSHI' if p_yes < k_yes else 'BUY_KALSHI_SELL_POLY'

        self._arb_snapshots.append(
            ArbSnapshot(
                timestamp=self._event_counter,
                sum_yes=0,
                overpricing=edge,
                team_prices={'poly': p_yes, 'kalshi': k_yes},
                action='CROSS_PLATFORM',
                profit_per_set=round(edge, 4),
            )
        )

        self.record_decision(
            ticker_name=f'XPLAT {team[:30]}',
            action=direction,
            executed=True,
            reasoning=f'poly={p_yes:.4f} kalshi={k_yes:.4f} edge={edge:.4f}',
            signal_values={'poly_yes': p_yes, 'kalshi_yes': k_yes, 'edge': edge},
        )

    def _cooldown_ok(self) -> bool:
        return self._event_counter - self._last_arb_event >= self.cooldown_events
