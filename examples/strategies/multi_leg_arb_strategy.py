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

from coinjure.events.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import PolyMarketTicker, Ticker
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide

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
    orders_placed: int = 0
    orders_filled: int = 0


class MultiLegArbStrategy(Strategy):
    """Detect and trade multi-leg arbitrage on multi-outcome events.

    Works in two modes:

    1. **Intra-platform multi-leg**: When sum(YES) > 1 + threshold on a
       single platform, short the overpriced portfolio.
    2. **Cross-platform**: When the same team is cheaper on one platform,
       buy cheap YES + sell expensive YES (buy NO on the expensive side).
    """

    name = 'multi_leg_arb'
    version = '2.0.0'
    author = 'coinjure'

    def __init__(
        self,
        min_overpricing: float = 0.015,
        min_cross_platform_edge: float = 0.02,
        trade_size: Decimal = Decimal('10'),
        cooldown_events: int = 5,
        ticker_metadata: dict[str, tuple[str, str]] | None = None,
        min_outcomes: int = 3,
    ) -> None:
        super().__init__()
        self.min_overpricing = min_overpricing
        self.min_cross_platform_edge = min_cross_platform_edge
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_events = cooldown_events
        self.min_outcomes = min_outcomes
        # symbol -> (platform, team) — set externally by the data source
        self._ticker_metadata: dict[str, tuple[str, str]] = ticker_metadata or {}

        # State — prices grouped by event_id for live mode
        self._prices: dict[str, dict[str, Decimal]] = {}  # platform -> {team: price}
        # event_id -> {outcome_name: Decimal price}
        self._event_prices: dict[str, dict[str, Decimal]] = {}
        # event_id -> number of price updates received (warmup tracking)
        self._event_updates: dict[str, int] = {}
        # event_id -> True if mutually exclusive (negRisk=True from Polymarket)
        self._event_exclusive: dict[str, bool] = {}
        self._event_counter = 0
        self._last_arb_event: int = -999
        self._arb_snapshots: list[ArbSnapshot] = []
        self._team_tickers: dict[str, Ticker] = {}  # team -> ticker

    @property
    def arb_snapshots(self) -> list[ArbSnapshot]:
        return list(self._arb_snapshots)

    def _extract_price(self, event: Event) -> tuple[Ticker, Decimal] | None:
        """Extract ticker and YES price from a supported event type."""
        if isinstance(event, PriceChangeEvent):
            return event.ticker, event.price
        if isinstance(event, OrderBookEvent) and event.side == 'bid':
            ticker = event.ticker
            # Only track YES-side tokens for sum(YES) calculation.
            if isinstance(ticker, PolyMarketTicker) and not ticker.is_yes:
                return None
            return ticker, event.price
        return None

    def _update_event_prices(self, ticker: Ticker, team: str, price: Decimal) -> str:
        """Track per-event prices in live mode.  Returns the event_id (or '')."""
        if self._ticker_metadata or not isinstance(ticker, PolyMarketTicker):
            return ''
        event_id = ticker.event_id
        if not event_id:
            return ''
        if event_id not in self._event_prices:
            self._event_prices[event_id] = {}
            self._event_updates[event_id] = 0
        self._event_prices[event_id][team] = price
        self._event_updates[event_id] += 1
        # Track whether this event is mutually exclusive (negRisk=True)
        if event_id not in self._event_exclusive:
            self._event_exclusive[event_id] = ticker.neg_risk
        return event_id

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        extracted = self._extract_price(event)
        if extracted is None:
            return
        ticker, price = extracted
        self._event_counter += 1

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

        event_id = self._update_event_prices(ticker, team, price)

        # Intra-platform arb check
        if self._event_prices:
            # Live mode: check each multi-outcome event separately.
            # Require at least 2 full rounds of updates per outcome to
            # avoid false signals during initial order book loading.
            if event_id and event_id in self._event_prices:
                # Only trade mutually exclusive events (negRisk=True)
                if not self._event_exclusive.get(event_id, False):
                    return
                ep = self._event_prices[event_id]
                n_outcomes = len(ep)
                n_updates = self._event_updates.get(event_id, 0)
                if n_outcomes >= self.min_outcomes and n_updates >= n_outcomes * 2:
                    await self._check_intra_platform_arb(event, trader, ep)
        else:
            # Backtest mode: use flat polymarket price dict
            poly_prices = self._prices.get('polymarket', {})
            await self._check_intra_platform_arb(event, trader, poly_prices)

        # Cross-platform arb
        poly_prices = self._prices.get('polymarket', {})
        self._check_cross_platform_arb(team, poly_prices)

    @staticmethod
    def _get_no_ticker(ticker: Ticker) -> PolyMarketTicker | None:
        """Get or synthesize the NO-side ticker."""
        if isinstance(ticker, PolyMarketTicker):
            no = ticker.get_no_ticker()
            if no is not None:
                return no
            # Synthesize NO ticker for backtest tickers without no_token_id
            return PolyMarketTicker(
                symbol=f'{ticker.symbol}_NO',
                name=ticker.name,
                token_id=f'{ticker.token_id or ticker.symbol}_NO',
                market_id=ticker.market_id,
                event_id=ticker.event_id,
                no_token_id=ticker.token_id or ticker.symbol,
                is_yes=False,
            )
        return None

    async def _execute_sell_overpriced(
        self, trader: Trader, poly_prices: dict[str, Decimal]
    ) -> tuple[int, int]:
        """Buy NO on every outcome to lock in overpricing profit.

        Returns (orders_placed, orders_filled).
        """
        placed = 0
        filled = 0
        for team, yes_price in poly_prices.items():
            ticker = self._team_tickers.get(team)
            if ticker is None:
                continue
            no_ticker = self._get_no_ticker(ticker)
            if no_ticker is None:
                continue
            # NO price = 1 - YES price
            no_price = Decimal('1') - yes_price
            if no_price <= 0:
                continue
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=no_ticker,
                limit_price=no_price,
                quantity=self.trade_size,
            )
            placed += 1
            if result.executed:
                filled += 1
        return placed, filled

    async def _execute_buy_underpriced(
        self, trader: Trader, poly_prices: dict[str, Decimal]
    ) -> tuple[int, int]:
        """Buy YES on every outcome to lock in underpricing profit.

        Returns (orders_placed, orders_filled).
        """
        placed = 0
        filled = 0
        for team, yes_price in poly_prices.items():
            ticker = self._team_tickers.get(team)
            if ticker is None or yes_price <= 0:
                continue
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=ticker,
                limit_price=yes_price,
                quantity=self.trade_size,
            )
            placed += 1
            if result.executed:
                filled += 1
        return placed, filled

    async def _check_intra_platform_arb(
        self, event: Event, trader: Trader, poly_prices: dict[str, Decimal]
    ) -> None:
        """Detect intra-platform multi-leg overpricing/underpricing."""
        if len(poly_prices) < max(self.min_outcomes, 3):
            return

        sum_yes = sum(float(p) for p in poly_prices.values())
        overpricing = sum_yes - 1.0
        n_teams = len(poly_prices)

        # Sanity checks for exclusive-event assumption:
        # - sum_yes < 0.85: likely missing outcomes, skip to avoid false signals
        # - sum_yes > 1.5: likely a non-exclusive event (e.g. "top 4 finish")
        #   where multiple outcomes can win simultaneously
        if sum_yes < 0.85 or sum_yes > 1.5:
            return

        if overpricing >= self.min_overpricing:
            if not self._cooldown_ok():
                return
            self._last_arb_event = self._event_counter

            # Execute: buy NO on every outcome
            placed, filled = await self._execute_sell_overpriced(trader, poly_prices)

            ts = getattr(event, 'timestamp', 0)
            if not isinstance(ts, int | float):
                ts = 0

            self._arb_snapshots.append(ArbSnapshot(
                timestamp=int(ts) if ts else self._event_counter,
                sum_yes=round(sum_yes, 4),
                overpricing=round(overpricing, 4),
                team_prices={k: round(float(v), 4) for k, v in list(poly_prices.items())[:5]},
                action='SELL_OVERPRICED',
                profit_per_set=round(overpricing, 4),
                orders_placed=placed,
                orders_filled=filled,
            ))

            self.record_decision(
                ticker_name=f'MULTI_LEG ({n_teams} teams)',
                action='SELL_OVERPRICED',
                executed=filled > 0,
                reasoning=(
                    f'sum_yes={sum_yes:.4f} overpricing={overpricing:.4f} '
                    f'orders={filled}/{placed}'
                ),
                signal_values={'sum_yes': sum_yes, 'overpricing': overpricing},
            )

            logger.info(
                'MULTI-LEG ARB: sum=%.4f overpricing=%.4f filled=%d/%d',
                sum_yes, overpricing, filled, placed,
            )

        elif overpricing < -self.min_overpricing:
            if not self._cooldown_ok():
                return
            self._last_arb_event = self._event_counter

            # Execute: buy YES on every outcome
            placed, filled = await self._execute_buy_underpriced(trader, poly_prices)

            self._arb_snapshots.append(ArbSnapshot(
                timestamp=self._event_counter,
                sum_yes=round(sum_yes, 4),
                overpricing=round(overpricing, 4),
                team_prices={k: round(float(v), 4) for k, v in list(poly_prices.items())[:5]},
                action='BUY_UNDERPRICED',
                profit_per_set=round(abs(overpricing), 4),
                orders_placed=placed,
                orders_filled=filled,
            ))

            self.record_decision(
                ticker_name=f'MULTI_LEG ({n_teams} teams)',
                action='BUY_UNDERPRICED',
                executed=filled > 0,
                reasoning=(
                    f'sum_yes={sum_yes:.4f} underpricing={abs(overpricing):.4f} '
                    f'orders={filled}/{placed}'
                ),
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

    def _check_cross_platform_arb(self, team: str, poly_prices: dict[str, Decimal]) -> None:
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

        self._arb_snapshots.append(ArbSnapshot(
            timestamp=self._event_counter,
            sum_yes=0,
            overpricing=edge,
            team_prices={'poly': p_yes, 'kalshi': k_yes},
            action='CROSS_PLATFORM',
            profit_per_set=round(edge, 4),
        ))

        self.record_decision(
            ticker_name=f'XPLAT {team[:30]}',
            action=direction,
            executed=True,
            reasoning=f'poly={p_yes:.4f} kalshi={k_yes:.4f} edge={edge:.4f}',
            signal_values={'poly_yes': p_yes, 'kalshi_yes': k_yes, 'edge': edge},
        )

    def _cooldown_ok(self) -> bool:
        return self._event_counter - self._last_arb_event >= self.cooldown_events
