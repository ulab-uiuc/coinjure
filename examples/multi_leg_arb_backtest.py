#!/usr/bin/env python3
"""
Multi-Leg Arbitrage Backtest — Real Polymarket + Kalshi Data
=============================================================

Backtests two arbitrage strategies on REAL market data fetched from
the Polymarket CLOB API and Kalshi trade API:

1. **Intra-platform multi-leg arb** (Polymarket only):
   In multi-outcome events (e.g., "2026 NBA Champion"), the sum of all
   YES prices should equal 1.0. When it exceeds 1.0, we can lock in
   risk-free profit by buying NO on every outcome.

2. **Cross-platform arb** (Polymarket vs Kalshi):
   The same team is priced differently on the two platforms. Buy YES
   on the cheaper platform and buy NO on the more expensive platform.

Data
----
``examples/data/nba_cross_platform_data.json`` contains ~670 hourly
price points per team from Polymarket and ~300-1400 from Kalshi,
covering Feb-Mar 2026, for all 30 NBA teams.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

from coinjure.engine.execution.paper_trader import PaperTrader
from coinjure.engine.execution.position_manager import Position, PositionManager
from coinjure.engine.execution.risk_manager import NoRiskManager
from coinjure.engine.trading_engine import TradingEngine
from coinjure.events import Event, PriceChangeEvent
from coinjure.market.data_source import DataSource
from coinjure.market.market_data_manager import MarketDataManager
from coinjure.ticker import CashTicker, PolyMarketTicker
from examples.strategies.multi_leg_arb_strategy import MultiLegArbStrategy


class CrossPlatformDataSource(DataSource):
    """Load real Polymarket + Kalshi price data from JSON and emit events.

    Creates interleaved PriceChangeEvent streams from both platforms,
    sorted by timestamp, so the strategy sees a realistic event flow.

    Metadata (platform, team) is stored in a separate lookup keyed by
    ticker symbol, since ``PolyMarketTicker`` is a frozen dataclass.
    """

    def __init__(self, data_path: str) -> None:
        with open(data_path) as f:
            raw = json.load(f)

        self.event_name = raw.get('event', 'Unknown')
        self._events: list[Event] = []
        self._index = 0
        self._tickers: dict[str, PolyMarketTicker] = {}
        # symbol -> (platform, team)
        self.ticker_metadata: dict[str, tuple[str, str]] = {}

        # Build Polymarket events
        poly_data = raw.get('polymarket', {})
        for team, ts_prices in poly_data.items():
            symbol = f'POLY_{team.replace(" ", "_")[:20]}'
            ticker = PolyMarketTicker(
                symbol=symbol,
                name=team,
                token_id=f'poly_{team}',
                market_id=f'poly_{team}',
                event_id='nba_2026',
                no_token_id=f'{symbol}_NO',
            )
            self._tickers[f'poly_{team}'] = ticker
            self.ticker_metadata[symbol] = ('polymarket', team)

            for ts_str, price in ts_prices.items():
                ts = int(ts_str)
                ev = PriceChangeEvent(
                    ticker=ticker,
                    price=Decimal(str(round(price, 4))),
                    timestamp=ts,
                )
                self._events.append(ev)

        # Build Kalshi events
        kalshi_data = raw.get('kalshi', {})
        for team, ts_prices in kalshi_data.items():
            symbol = f'KALSHI_{team.replace(" ", "_")[:18]}'
            ticker = PolyMarketTicker(
                symbol=symbol,
                name=team,
                token_id=f'kalshi_{team}',
                market_id=f'kalshi_{team}',
                event_id='nba_2026_kalshi',
                no_token_id=f'{symbol}_NO',
            )
            self._tickers[f'kalshi_{team}'] = ticker
            self.ticker_metadata[symbol] = ('kalshi', team)

            for ts_str, price in ts_prices.items():
                ts = int(ts_str)
                ev = PriceChangeEvent(
                    ticker=ticker,
                    price=Decimal(str(round(price, 4))),
                    timestamp=ts,
                )
                self._events.append(ev)

        # Sort by timestamp
        self._events.sort(
            key=lambda e: (e.timestamp if isinstance(e.timestamp, int | float) else 0)
        )

        print(f'  Loaded {len(self._events):,} events for "{self.event_name}"')
        print(
            f'  Polymarket: {len(poly_data)} teams, {sum(len(v) for v in poly_data.values()):,} data points'
        )
        print(
            f'  Kalshi:     {len(kalshi_data)} teams, {sum(len(v) for v in kalshi_data.values()):,} data points'
        )

    async def get_next_event(self) -> Event | None:
        if self._index < len(self._events):
            ev = self._events[self._index]
            self._index += 1
            return ev
        return None


def _analyze_intra_platform(poly_data: dict) -> None:  # noqa: C901
    """Analyze intra-platform overpricing in Polymarket multi-outcome event."""
    all_timestamps: set[int] = set()
    for _team, ts_prices in poly_data.items():
        all_timestamps.update(int(t) for t in ts_prices.keys())
    timestamps = sorted(all_timestamps)

    overpricing_series: list[tuple[int, float]] = []
    for ts in timestamps:
        total = 0.0
        count = 0
        for _team, ts_prices in poly_data.items():
            ts_str = str(ts)
            if ts_str in ts_prices:
                total += ts_prices[ts_str]
                count += 1
            else:
                closest = min(
                    ts_prices.keys(), key=lambda t: abs(int(t) - ts), default=None
                )
                if closest and abs(int(closest) - ts) < 7200:
                    total += ts_prices[closest]
                    count += 1
        if count >= 20:
            overpricing_series.append((ts, total - 1.0))

    if not overpricing_series:
        return

    ops = [op for _, op in overpricing_series]
    arb_count = sum(1 for op in ops if op > 0.015)
    underpriced_count = sum(1 for op in ops if op < -0.015)
    total_arb_profit = sum(op for op in ops if op > 0.015)

    print('\n  Intra-Platform Analysis (Polymarket):')
    print(f'    Time points analyzed:   {len(overpricing_series):,}')
    print(f'    Avg overpricing:        {sum(ops)/len(ops):+.4f}')
    print(f'    Max overpricing:        {max(ops):+.4f}')
    print(f'    Min overpricing:        {min(ops):+.4f}')
    print(f'    Arb signals (>1.5%):    {arb_count} ({100*arb_count/len(ops):.1f}%)')
    print(f'    Underpriced (<-1.5%):   {underpriced_count}')
    print(f'    Total arb edge (sum):   {total_arb_profit:.4f}')
    print(f'    With $10/leg, ~profit:  ${total_arb_profit * 10:.2f}')


def _analyze_cross_platform(poly_data: dict, kalshi_data: dict) -> None:
    """Analyze cross-platform price discrepancies."""
    common_teams = set(poly_data.keys()) & set(kalshi_data.keys())
    cross_arbs = 0
    cross_edge_sum = 0.0

    for team in common_teams:
        poly_ts = poly_data[team]
        kalshi_ts = kalshi_data[team]
        for ts_str, p_price in poly_ts.items():
            ts = int(ts_str)
            closest_k = min(
                kalshi_ts.keys(), key=lambda t: abs(int(t) - ts), default=None
            )
            if closest_k and abs(int(closest_k) - ts) < 3600:
                k_price = kalshi_ts[closest_k]
                edge = abs(p_price - k_price)
                if edge >= 0.02:
                    cross_arbs += 1
                    cross_edge_sum += edge

    if cross_arbs:
        print('\n  Cross-Platform Analysis (Polymarket vs Kalshi):')
        print(f'    Teams with overlap:     {len(common_teams)}')
        print(f'    Cross-arb signals:      {cross_arbs:,}')
        print(f'    Avg edge per signal:    {cross_edge_sum/cross_arbs:.4f}')
        print(f'    Total edge sum:         {cross_edge_sum:.4f}')
        print(f'    With $10/trade, ~profit: ${cross_edge_sum * 10:.2f}')


def _analyze_arb_opportunities(data_path: str) -> None:
    """Pre-analyze the data to show real arbitrage opportunities."""
    with open(data_path) as f:
        raw = json.load(f)

    poly_data = raw.get('polymarket', {})
    kalshi_data = raw.get('kalshi', {})

    _analyze_intra_platform(poly_data)
    _analyze_cross_platform(poly_data, kalshi_data)


async def run_backtest() -> None:
    """Run the multi-leg arb backtest on real data."""
    print('=' * 64)
    print('  Multi-Leg Arbitrage Backtest — Real Market Data')
    print('=' * 64)

    # Locate data file
    data_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(data_dir, 'data', 'nba_cross_platform_data.json')

    if not os.path.exists(data_path):
        print(f'\nData file not found: {data_path}')
        print('Run the data collection script first.')
        return

    print(f'\nData file: {data_path}')
    print(f'File size: {os.path.getsize(data_path) / 1024:.1f} KB')

    # Pre-analysis
    _analyze_arb_opportunities(data_path)

    # --- Set up backtest ---
    initial_capital = Decimal('10000')

    data_source = CrossPlatformDataSource(data_path)

    market_data = MarketDataManager()

    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.9'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Strategy: detect 1.5% overpricing, 2% cross-platform edge
    strategy = MultiLegArbStrategy(
        min_overpricing=0.015,
        min_cross_platform_edge=0.02,
        trade_size=Decimal('10'),
        cooldown_events=5,
        ticker_metadata=data_source.ticker_metadata,
    )

    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
    )

    print(f'\n  Strategy:         {strategy.name} v{strategy.version}')
    print(f'  Min overpricing:  {strategy.min_overpricing*100:.1f}%')
    print(f'  Min cross edge:   {strategy.min_cross_platform_edge*100:.1f}%')
    print(f'  Trade size:       ${strategy.trade_size}/leg')
    print(f'  Initial capital:  ${initial_capital:,.2f}')

    print('\n  Starting backtest...\n')
    await engine.start()

    # --- Results ---
    print('\n' + '=' * 64)
    print('  Backtest Results')
    print('=' * 64)

    stats = strategy.get_decision_stats()
    arb_snaps = strategy.arb_snapshots

    print(f'\n  Total decisions:       {stats["decisions"]}')
    print(f'  Executed trades:       {stats["executed"]}')
    print(f'  Holds:                 {stats["holds"]}')

    # Arb breakdown
    intra_arbs = [s for s in arb_snaps if s.action == 'SELL_OVERPRICED']
    cross_arbs = [s for s in arb_snaps if s.action == 'CROSS_PLATFORM']
    under_arbs = [s for s in arb_snaps if s.action == 'BUY_UNDERPRICED']

    print('\n  Arb opportunities detected:')
    print(f'    Intra-platform (overpriced): {len(intra_arbs)}')
    print(f'    Cross-platform:              {len(cross_arbs)}')
    print(f'    Underpriced:                 {len(under_arbs)}')

    if intra_arbs:
        avg_op = sum(s.overpricing for s in intra_arbs) / len(intra_arbs)
        max_op = max(s.overpricing for s in intra_arbs)
        total_profit = sum(s.profit_per_set for s in intra_arbs)
        print('\n  Intra-Platform Arb Stats:')
        print(f'    Avg overpricing:   {avg_op:.4f} ({avg_op*100:.2f}%)')
        print(f'    Max overpricing:   {max_op:.4f} ({max_op*100:.2f}%)')
        print(f'    Total edge (sum):  {total_profit:.4f}')
        print(f'    Est. profit ($10/leg): ${total_profit * 10:.2f}')

    if cross_arbs:
        avg_edge = sum(s.overpricing for s in cross_arbs) / len(cross_arbs)
        max_edge = max(s.overpricing for s in cross_arbs)
        total_profit = sum(s.profit_per_set for s in cross_arbs)
        print('\n  Cross-Platform Arb Stats:')
        print(f'    Avg edge:          {avg_edge:.4f} ({avg_edge*100:.2f}%)')
        print(f'    Max edge:          {max_edge:.4f} ({max_edge*100:.2f}%)')
        print(f'    Total edge (sum):  {total_profit:.4f}')
        print(f'    Est. profit ($10/trade): ${total_profit * 10:.2f}')

    # Position summary
    cash_pos = position_manager.get_cash_positions()
    non_cash = position_manager.get_non_cash_positions()
    total_realized = position_manager.get_total_realized_pnl()

    print('\n  Portfolio:')
    for pos in cash_pos:
        print(f'    Cash ({pos.ticker.name}): ${pos.quantity:,.2f}')
    print(f'    Open positions:    {len(non_cash)}')
    print(f'    Realized PnL:      ${total_realized:,.2f}')

    # Combined summary
    all_arb = intra_arbs + cross_arbs
    if all_arb:
        combined_edge = sum(s.profit_per_set for s in all_arb)
        trade_size_val = float(strategy.trade_size)
        est_gross = combined_edge * trade_size_val
        est_return = est_gross / float(initial_capital) * 100

        print('\n  Combined Strategy Performance:')
        print(f'    Total arb signals:     {len(all_arb):,}')
        print(f'    Combined edge:         {combined_edge:.4f}')
        print(
            f'    Est. gross profit:     ${est_gross:,.2f} (at ${trade_size_val:.0f}/trade)'
        )
        print(f'    Est. return on capital: {est_return:.2f}%')
        print('    Data period:           ~30 days (Feb-Mar 2026)')
        if est_return > 0:
            annualized = est_return * 365 / 30
            print(f'    Annualized (est.):     {annualized:.1f}%')

    # Show sample arb snapshots
    if arb_snaps:
        # Filter out initialization artifacts
        real_snaps = [
            s for s in arb_snaps if s.action in ('SELL_OVERPRICED', 'CROSS_PLATFORM')
        ]
        print('\n  Sample Arb Snapshots (first 15):')
        print(f'  {"#":>3s}  {"Action":20s}  {"Edge":>10s}  {"Profit/Set":>12s}')
        print('  ' + '-' * 50)
        for i, snap in enumerate(real_snaps[:15]):
            print(
                f'  {i+1:3d}  {snap.action:20s}  {snap.overpricing:10.4f}  '
                f'{snap.profit_per_set:12.4f}'
            )

    print('\n  Backtest complete!')
    print()


if __name__ == '__main__':
    asyncio.run(run_backtest())
