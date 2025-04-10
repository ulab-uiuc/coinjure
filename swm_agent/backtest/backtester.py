import asyncio
from decimal import Decimal

from swm_agent.core.trading_engine import TradingEngine
from swm_agent.data.backtest.historical_data_source import HistoricalDataSource
from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import NoRiskManager
from swm_agent.strategy.simple_strategy import SimpleStrategy
from swm_agent.strategy.strategy import Strategy
from swm_agent.ticker.ticker import CashTicker
from swm_agent.trader.paper_trader import PaperTrader


async def run_backtest(
    history_file: str, initial_capital: Decimal, strategy: Strategy
) -> None:
    data_source = HistoricalDataSource(history_file)
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
    # TODO: performance analyzing
    print('Backtest complete.')


if __name__ == '__main__':
    asyncio.run(run_backtest('', Decimal('10000'), SimpleStrategy()))
