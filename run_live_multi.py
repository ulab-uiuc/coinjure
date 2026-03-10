#!/usr/bin/env python3
"""Multi-strategy live trading: Kalshi + Polymarket.

Covers all available arbitrage opportunities:
  - Kalshi implication arb: Trump out (10), Insurrection (3), Greenland (3)
  - Polymarket implication arb: top liquid pairs
  - Polymarket group arb: exclusivity/complementary

Trade size: $1/leg. Runs continuously.
"""
import asyncio
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('live_multi')

from coinjure.data.live.kalshi import LiveKalshiDataSource
from coinjure.data.live.polymarket import LivePolyMarketDataSource
from coinjure.data.manager import DataManager
from coinjure.engine.runner import run_live_trading
from coinjure.engine.trader.kalshi import KalshiTrader
from coinjure.engine.trader.paper import PaperTrader
from coinjure.strategy.builtin.implication_arb_strategy import ImplicationArbStrategy
from coinjure.strategy.builtin.group_arb_strategy import GroupArbStrategy
from coinjure.strategy.builtin.conditional_arb_strategy import ConditionalArbStrategy
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import CashTicker
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import StandardRiskManager, NoRiskManager

# ── Kalshi series to watch ───────────────────────────────────────────────
KALSHI_WATCH_SERIES = [
    # Political — calendar arb
    'KXTRUMPOUT27', 'KXINSURRECTION', 'KXGREENTERRITORY',
    # Political — group arb
    'KXSTATE51', 'KXNEXTPRESSEC', 'KXSCOTUSPOWER',
    'KXPARDONSTRUMP', 'KXTRUTHSOCIAL', 'KXTRUMPFIRE',
    'KXTRUMPAGCOUNT', 'KXEOTRUMPTERM', 'KXNEXTSTATE',
    'KXNEXTDEF', 'KXNEXTDHSSEC', 'KXBILLSCOUNT',
    'KXGOLDCARDS', 'KXAPRPOTUS', 'KXGREENLANDPRICE',
    # Economic — group arb
    'KXFEDDECISION', 'KXFEDCHAIRCONFIRM', 'KXFEDCHGCOUNT',
    'KXFED', 'KXFOMCDISSENTCOUNT', 'KXFEDGOVNOM',
    'KXTARIFFRATECAN', 'KXTARIFFRATEEU', 'KXTARIFFRATEPRC',
    'KXTARIFFRATEINDIA', 'KXGDPYEAR', 'KXGDPNOM',
    'KXGOVTCUTS', 'KXLAGODAYS',
    # Implication — thresholds
    'KXAGENCIES', 'KXBTCMAX100',
]

# ── Kalshi implication strategies ────────────────────────────────────────
KALSHI_IMPL_RELATIONS = [
    # Trump out: all 10 valid calendar pairs
    'KXTRUMPOUT27-27-26APR01-KXTRUMPOUT27-27-26AUG01',
    'KXTRUMPOUT27-27-26APR01-KXTRUMPOUT27-27-DJT',
    'KXTRUMPOUT27-27-26APR01-KXTRUMPOUT27-27-28',
    'KXTRUMPOUT27-27-26APR01-KXTRUMPOUT27-27-JAN2029',
    'KXTRUMPOUT27-27-26AUG01-KXTRUMPOUT27-27-DJT',
    'KXTRUMPOUT27-27-26AUG01-KXTRUMPOUT27-27-28',
    'KXTRUMPOUT27-27-26AUG01-KXTRUMPOUT27-27-JAN2029',
    'KXTRUMPOUT27-27-DJT-KXTRUMPOUT27-27-28',
    'KXTRUMPOUT27-27-DJT-KXTRUMPOUT27-27-JAN2029',
    'KXTRUMPOUT27-27-28-KXTRUMPOUT27-27-JAN2029',
    # Insurrection Act: 3 calendar pairs
    'KXINSURRECTION-29-26MAY-KXINSURRECTION-29-27',
    'KXINSURRECTION-29-26MAY-KXINSURRECTION-29',
    'KXINSURRECTION-29-27-KXINSURRECTION-29',
    # Greenland acquisition: 3 calendar pairs
    'KXGREENTERRITORY-29-26APR-KXGREENTERRITORY-29-27',
    'KXGREENTERRITORY-29-26APR-KXGREENTERRITORY-29',
    'KXGREENTERRITORY-29-27-KXGREENTERRITORY-29',
    # DOGE agency cuts: threshold nesting (>=10 implies >=5)
    'KXAGENCI-KXAGENCI',
]

# ── Kalshi group arb (complementary/exclusivity) ──────────────────────
KALSHI_GROUP_RELATIONS = [
    # Tariff rates (high sums = strong BUY_NO edge)
    'comp-KXTARIFFRATEPRC-26JUL01-14-KXTARIFFRATEPRC-26JUL01-24-KXTARIFFRATEPRC-26JUL01-30-+4',
    'comp-KXTARIFFRATECAN-26JUL01-14-KXTARIFFRATECAN-26JUL01-24-KXTARIFFRATECAN-26JUL01-30-+3',
    'comp-KXTARIFFRATEEU-26JUL01-14-KXTARIFFRATEEU-26JUL01-24-KXTARIFFRATEEU-26JUL01-30-+4',
    'comp-KXTARIFFRATEINDIA-26JUL01-14-KXTARIFFRATEINDIA-26JUL01-24-KXTARIFFRATEINDIA-26JUL01-30-+4',
    # Political
    'comp-KXPARDONSTRUMP-26MAR-0-KXPARDONSTRUMP-26MAR-1-KXPARDONSTRUMP-26MAR-12-+5',
    'comp-KXEOTRUMPTERM-29JAN20-300-KXEOTRUMPTERM-29JAN20-324-KXEOTRUMPTERM-29JAN20-374-+9',
    'comp-KXTRUMPFIRE-27-0-KXTRUMPFIRE-27-1-KXTRUMPFIRE-27-2-+3',
    'comp-KXSCOTUSPOWER-29-29JAN20-R0-0-KXSCOTUSPOWER-29-29JAN20-R6-3-KXSCOTUSPOWER-29-29JAN20-R7-2-+2',
    'comp-KXTRUMPAGCOUNT-29-1-KXTRUMPAGCOUNT-29-2-KXTRUMPAGCOUNT-29-3-+2',
    'comp-KXGOVTCUTS-28-1000-KXGOVTCUTS-28-2000-KXGOVTCUTS-28-250-+2',
    'comp-KXGOLDCARDS-26-B0.0-KXGOLDCARDS-26-B3.0-KXGOLDCARDS-26-B52.5-+1',
    'comp-KXNEXTSTATE-29-BHAG-KXNEXTSTATE-29-NONE-KXNEXTSTATE-29-RGRE-+1',
    'comp-KXNEXTDEF-29-JERN-KXNEXTDEF-29-NONE-KXNEXTDEF-29-RDES-+3',
    # Economic
    'comp-KXGDPYEAR-26-B0.3-KXGDPYEAR-26-B0.8-KXGDPYEAR-26-B1.3-+7',
    'comp-KXGDPNOM-IND26-4.0-KXGDPNOM-IND26-4.2-KXGDPNOM-IND26-4.4-+1',
    'comp-KXBILLSCOUNT-26MAR-0.0-KXBILLSCOUNT-26MAR-1.0-KXBILLSCOUNT-26MAR-2.0-+5',
    # Fed decisions (multiple meeting dates)
    'comp-KXFEDDECISION-26MAR-C25-KXFEDDECISION-26MAR-H0',
    'comp-KXFEDDECISION-26APR-C25-KXFEDDECISION-26APR-C26-KXFEDDECISION-26APR-H0-+1',
    'comp-KXFEDDECISION-26JUN-C25-KXFEDDECISION-26JUN-C26-KXFEDDECISION-26JUN-H0-+1',
    'comp-KXFEDDECISION-26SEP-C25-KXFEDDECISION-26SEP-H0-KXFEDDECISION-26SEP-H25',
    'comp-KXFEDDECISION-26OCT-C25-KXFEDDECISION-26OCT-H0-KXFEDDECISION-26OCT-H25',
    'comp-KXFEDDECISION-26DEC-C25-KXFEDDECISION-26DEC-H0-KXFEDDECISION-26DEC-H25',
    'comp-KXFEDDECISION-27JAN-C25-KXFEDDECISION-27JAN-H0',
    'comp-KXFEDDECISION-27MAR-C25-KXFEDDECISION-27MAR-H0-KXFEDDECISION-27MAR-H25',
    'comp-KXFED-26DEC-T2.75-KXFED-26DEC-T3.00-KXFED-26DEC-T3.25-+1',
    'comp-KXFEDCHAIRCONFIRM-JSHE-KXFEDCHAIRCONFIRM-KWAR',
    'comp-KXFOMCDISSENTCOUNT-26MAR-1-KXFOMCDISSENTCOUNT-26MAR-2-KXFOMCDISSENTCOUNT-26MAR-3-+1',
    # Exclusivity
    'excl-KXNEXTPRESSEC-29JAN21-AHAB-KXNEXTPRESSEC-29JAN21-KLAK-KXNEXTPRESSEC-29JAN21-NNP-+2',
    'excl-KXSTATE51-29-CA-KXSTATE51-29-GL-KXSTATE51-29-PR-+1',
    # Misc
    'comp-KXLAGODAYS-26MAR-1-KXLAGODAYS-26MAR-2-KXLAGODAYS-26MAR-3-+2',
    'comp-KXPARDONSYEAR-26DEC-299-KXPARDONSYEAR-26DEC-37-KXPARDONSYEAR-26DEC-74-+2',
    'comp-KXGREENLANDPRICE-29JAN21-1049B-KXGREENLANDPRICE-29JAN21-1200B-KXGREENLANDPRICE-29JAN21-199B-+5',
    'comp-KXBTCMAX100-26-APR-KXBTCMAX100-26-DEC-KXBTCMAX100-26-JUNE-+3',
]

# ── Polymarket strategies ────────────────────────────────────────────────
# Top Polymarket exclusivity/complementary relations (GroupArbStrategy)
POLY_GROUP_RELATIONS = [
    # High-volume exclusivity pairs
    '564213-564216',
    '564199-564204',
    # Complementary
    '516926-692250',
]

# Top Polymarket implication pairs
POLY_IMPL_RELATIONS = [
    '521532-1359701',   # Starmer out by date X → out ever
    '1336699-1359701',  # Starmer
    '623939-597964',    # Macron exit
    '517231-597964',    # Macron
    '517548-598936',    # UK election
    '598936-517550',    # UK election
    '522057-610379',    # Ukraine sovereignty
    '523343-734115',    # Ukraine election
]


class MultiStrategy(Strategy):
    """Fan-out wrapper: dispatches events to multiple sub-strategies."""

    name = 'multi_strategy'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(self, strategies: list[Strategy]) -> None:
        super().__init__()
        self.strategies = strategies

    async def process_event(self, event, trader) -> None:
        for strat in self.strategies:
            try:
                strat.bind_context(event, trader)
                await strat.process_event(event, trader)
            except Exception as e:
                logger.debug('Strategy %s error: %s', getattr(strat, 'name', '?'), e)

    def get_decisions(self):
        all_decisions = []
        for strat in self.strategies:
            all_decisions.extend(strat.get_decisions())
        return all_decisions

    def get_decision_stats(self):
        total = sum(len(s.get_decisions()) for s in self.strategies)
        return {'decisions': total}

    def watch_tokens(self) -> list[str]:
        tokens = []
        for strat in self.strategies:
            tokens.extend(strat.watch_tokens())
        return tokens


def _build_strategies(relation_ids: list[str], cls, **common_kwargs) -> list[Strategy]:
    """Build strategies, skipping relations that fail to load."""
    strategies = []
    for rid in relation_ids:
        try:
            strat = cls(relation_id=rid, **common_kwargs)
            if hasattr(strat, '_ids') and not strat._ids:
                logger.warning('Skipping %s: relation not found', rid)
                continue
            strategies.append(strat)
        except Exception as e:
            logger.warning('Skipping %s: %s', rid, e)
    return strategies


async def run_kalshi_engine():
    """Run Kalshi live trading engine with all implication arb strategies."""
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key_path = os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not api_key_id or not private_key_path:
        logger.error('Kalshi credentials not set, skipping Kalshi engine')
        return

    data_source = LiveKalshiDataSource(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        event_cache_file='kalshi_events_cache.jsonl',
        polling_interval=60.0,
        reprocess_on_start=False,
        watch_series=KALSHI_WATCH_SERIES,
    )

    market_data = DataManager()
    position_manager = PositionManager()
    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal('2'),
        max_position_size=Decimal('5'),
        max_total_exposure=Decimal('10'),
        max_drawdown_pct=Decimal('0.3'),
    )

    trader = KalshiTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        api_key_id=api_key_id,
        private_key_path=private_key_path,
    )

    # Fetch balance
    balance_resp = await asyncio.to_thread(
        lambda: trader._portfolio_api.get_balance()
    )
    balance = Decimal(str(balance_resp.balance)) / Decimal('100')
    position_manager.update_position(
        Position(
            ticker=CashTicker.KALSHI_USD,
            quantity=balance,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    # Build strategies
    strategies: list[Strategy] = []

    # Implication arb (calendar spreads)
    impl_strats = _build_strategies(
        KALSHI_IMPL_RELATIONS,
        ImplicationArbStrategy,
        trade_size=1.0,
        min_edge=0.01,
    )
    strategies.extend(impl_strats)

    # Group arb (complementary/exclusivity — sums that deviate from 1.0)
    for rid in KALSHI_GROUP_RELATIONS:
        try:
            s = GroupArbStrategy(relation_id=rid, trade_size=1.0, min_edge=0.02)
            if s._relation_market_ids:
                strategies.append(s)
            else:
                logger.warning('Skipping group %s: no market IDs', rid)
        except Exception as e:
            logger.warning('Skipping group %s: %s', rid, e)

    if not strategies:
        logger.error('No Kalshi strategies loaded')
        return

    multi = MultiStrategy(strategies)

    n_impl = len(impl_strats)
    n_group = len(strategies) - n_impl
    logger.info(
        'KALSHI ENGINE: $%s balance, %d strategies (%d impl + %d group), %d series',
        balance, len(strategies), n_impl, n_group, len(KALSHI_WATCH_SERIES),
    )
    for s in impl_strats:
        logger.info('  Kalshi impl: %s → %s', s.relation_id, s._ids)

    await run_live_trading(
        data_source=data_source,
        strategy=multi,
        trader=trader,
        continuous=True,
        monitor=False,
        exchange_name='Kalshi',
        socket_path=Path.home() / '.coinjure' / f'engine-kalshi-{os.getpid()}.sock',
    )


async def run_polymarket_engine():
    """Run Polymarket paper trading engine with group + implication arb."""
    private_key = os.environ.get('POLYMARKET_PRIVATE_KEY')
    if not private_key:
        logger.error('Polymarket credentials not set, skipping Polymarket engine')
        return

    data_source = LivePolyMarketDataSource(
        event_cache_file='poly_events_cache.jsonl',
        polling_interval=60.0,
        orderbook_refresh_interval=15.0,
        reprocess_on_start=False,
    )

    market_data = DataManager()
    position_manager = PositionManager()

    # Paper trading for Polymarket (safer to start)
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Seed cash
    initial_capital = Decimal('100')
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )

    # Build strategies
    strategies: list[Strategy] = []

    # Group arb (exclusivity/complementary)
    for rid in POLY_GROUP_RELATIONS:
        try:
            s = GroupArbStrategy(relation_id=rid, trade_size=5.0, min_edge=0.02)
            if s._relation_market_ids:
                strategies.append(s)
                logger.info('  Poly group: %s (%d markets)', rid, len(s._relation_market_ids))
        except Exception as e:
            logger.warning('Skipping poly group %s: %s', rid, e)

    # Implication arb
    poly_impl = _build_strategies(
        POLY_IMPL_RELATIONS,
        ImplicationArbStrategy,
        trade_size=5.0,
        min_edge=0.01,
    )
    strategies.extend(poly_impl)

    if not strategies:
        logger.error('No Polymarket strategies loaded')
        return

    multi = MultiStrategy(strategies)

    logger.info(
        'POLYMARKET ENGINE: $%s paper, %d strategies',
        initial_capital, len(strategies),
    )

    await run_live_trading(
        data_source=data_source,
        strategy=multi,
        trader=trader,
        continuous=True,
        monitor=False,
        exchange_name='Polymarket',
        socket_path=Path.home() / '.coinjure' / f'engine-poly-{os.getpid()}.sock',
    )


async def main():
    n_kalshi = len(KALSHI_IMPL_RELATIONS) + len(KALSHI_GROUP_RELATIONS)
    n_poly = len(POLY_GROUP_RELATIONS) + len(POLY_IMPL_RELATIONS)
    print('=' * 60)
    print('  FULL COVERAGE Multi-Exchange Live Trading')
    print('  Kalshi: real ($10) | Polymarket: paper ($100)')
    print(f'  Kalshi: {len(KALSHI_IMPL_RELATIONS)} implication + {len(KALSHI_GROUP_RELATIONS)} group arbs')
    print(f'  Polymarket: {len(POLY_GROUP_RELATIONS)} group + {len(POLY_IMPL_RELATIONS)} implication')
    print(f'  Total: {n_kalshi + n_poly} relation strategies')
    print('=' * 60)

    # Run both engines concurrently
    results = await asyncio.gather(
        run_kalshi_engine(),
        run_polymarket_engine(),
        return_exceptions=True,
    )
    for i, result in enumerate(results):
        name = ['Kalshi', 'Polymarket'][i]
        if isinstance(result, Exception):
            logger.error('%s engine failed: %s', name, result, exc_info=result)


if __name__ == '__main__':
    asyncio.run(main())
