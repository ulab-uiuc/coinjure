import asyncio
import logging
import os
from decimal import Decimal

from coinjure.core.trading_engine import TradingEngine
from coinjure.data.backtest.historical_data_source import HistoricalDataSource
from coinjure.data.market_data_manager import MarketDataManager
from coinjure.position.position_manager import Position, PositionManager
from coinjure.risk.risk_manager import NoRiskManager
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import CashTicker, PolyMarketTicker
from coinjure.trader.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


async def run_backtest(
    history_file: str,
    ticker_symbol: PolyMarketTicker,
    initial_capital: Decimal,
    strategy: Strategy,
    spread: Decimal = Decimal('0.01'),
    *,
    include_all_markets_context: bool = False,
    allow_cross_market_trading: bool = False,
) -> None:
    data_source = HistoricalDataSource(
        history_file,
        ticker_symbol,
        include_all_markets=include_all_markets_context,
    )
    market_data = MarketDataManager(
        spread=spread,
        max_history_per_ticker=None,
        max_timeline_events=None,
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
    if not allow_cross_market_trading:
        tradable_tickers = [ticker_symbol]
        no_ticker = ticker_symbol.get_no_ticker()
        if no_ticker is not None:
            tradable_tickers.append(no_ticker)
        trader.set_allowed_tickers(tradable_tickers)

    engine = TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    await engine.start()
    engine._perf.print_summary()
    logger.info('Backtest complete.')


async def run_backtest_parquet(
    parquet_path: str,
    initial_capital: Decimal,
    strategy: Strategy,
    market_id: str | None = None,
) -> None:
    from coinjure.data.backtest.parquet_data_source import ParquetDataSource

    data_source = ParquetDataSource(parquet_path, market_id=market_id)
    market_data = MarketDataManager(
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


if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    asyncio.run(
        run_backtest(
            os.path.join(current_dir, 'polymarket_data_processed_Crypto_test.jsonl'),
            PolyMarketTicker(
                symbol='poly_test',
                name='test_ticker',
                market_id='514893',
                event_id='15088',
            ),
            Decimal('10000'),
            TestStrategy(),
        )
    )
