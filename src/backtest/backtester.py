import asyncio
from decimal import Decimal
from core.trading_engine import TradingEngine
from data.backtest.historical_data_source import HistoricalDataSource
from strategy.strategy import Strategy
from strategy.simple_strategy import SimpleStrategy
from trader.paper_trader import PaperTrader
from data.market_data_manager import MarketDataManager
from risk.risk_manager import NoRiskManager
from position.position_manager import PositionManager


async def run_backtest(history_file: str, initial_capital: Decimal, strategy: Strategy):
    data_source = HistoricalDataSource(history_file)
    market_data = MarketDataManager()
    # add initial capital to position manager
    position_manager = PositionManager()
    risk_manager = NoRiskManager()
    
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0')
    )
    
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader
    )
    
    await engine.start()
    # TODO: performance analyzing
    print("Backtest complete.")


if __name__ == '__main__':
    asyncio.run(run_backtest("", Decimal('10000'), SimpleStrategy()))