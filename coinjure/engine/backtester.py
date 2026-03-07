import asyncio
import logging
import os
from decimal import Decimal

from coinjure.data.data_manager import DataManager
from coinjure.engine.trader.paper_trader import PaperTrader
from coinjure.engine.trader.position_manager import Position, PositionManager
from coinjure.engine.trader.risk_manager import NoRiskManager
from coinjure.engine.trading_engine import TradingEngine
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import CashTicker

logger = logging.getLogger(__name__)


async def run_backtest_parquet(
    parquet_path: str | list[str],
    initial_capital: Decimal,
    strategy: Strategy,
    market_id: str | None = None,
    market_ids: list[str] | None = None,
) -> None:
    from coinjure.data.backtest.parquet_data_source import ParquetDataSource

    data_source = ParquetDataSource(
        parquet_path, market_id=market_id, market_ids=market_ids
    )
    market_data = DataManager(
        spread=Decimal('0'),
        max_history_per_ticker=None,
        max_timeline_events=None,
        synthetic_book=False,
    )
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    risk_manager = NoRiskManager()

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    engine = TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    await engine.start()
    engine._perf.print_summary()
    logger.info('Parquet backtest complete.')
