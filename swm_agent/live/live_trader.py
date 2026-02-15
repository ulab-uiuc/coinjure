import asyncio
from decimal import Decimal

from swm_agent.core.trading_engine import TradingEngine
from swm_agent.data.live.live_data_source import (
    LiveNewsDataSource,
    LivePolyMarketDataSource,
    LiveRSSNewsDataSource,
)
from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import NoRiskManager, RiskManager, StandardRiskManager
from swm_agent.strategy.strategy import Strategy
from swm_agent.ticker.ticker import CashTicker
from swm_agent.trader.paper_trader import PaperTrader
from swm_agent.trader.polymarket_trader import PolymarketTrader
from swm_agent.trader.trader import Trader


async def run_live_trading(
    data_source: LivePolyMarketDataSource | LiveNewsDataSource | LiveRSSNewsDataSource,
    strategy: Strategy,
    trader: Trader,
    duration: float | None = None,
) -> None:
    """
    Run live trading with the given data source, strategy, and trader.

    Args:
        data_source: The live data source to use (Polymarket, News API, or RSS)
        strategy: The trading strategy to execute
        trader: The trader implementation (Paper or Polymarket)
        duration: Optional duration in seconds to run. If None, runs indefinitely.
    """
    engine = TradingEngine(data_source=data_source, strategy=strategy, trader=trader)

    # Start the live data source polling
    await data_source.start()

    if duration:
        # Run for specified duration
        try:
            await asyncio.wait_for(engine.start(), timeout=duration)
        except asyncio.TimeoutError:
            engine.stop()
            print(f'Live trading stopped after {duration} seconds.')
    else:
        # Run indefinitely
        await engine.start()

    print('Live trading session ended.')


async def run_live_paper_trading(
    data_source: LivePolyMarketDataSource | LiveNewsDataSource | LiveRSSNewsDataSource,
    strategy: Strategy,
    initial_capital: Decimal,
    risk_manager: RiskManager | None = None,
    duration: float | None = None,
) -> None:
    """
    Run live paper trading (simulated) with the given configuration.

    Args:
        data_source: The live data source to use
        strategy: The trading strategy to execute
        initial_capital: Starting capital in USDC
        risk_manager: Optional risk manager (defaults to NoRiskManager)
        duration: Optional duration in seconds to run
    """
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

    if risk_manager is None:
        risk_manager = NoRiskManager()

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    await run_live_trading(data_source, strategy, trader, duration)

    # Print final portfolio status
    print('\n--- Final Portfolio Status ---')
    print(f'Cash positions: {position_manager.get_cash_positions()}')
    print(f'Non-cash positions: {position_manager.get_non_cash_positions()}')
    print(f'Total realized PnL: {position_manager.get_total_realized_pnl()}')


async def run_live_polymarket_trading(
    data_source: LivePolyMarketDataSource,
    strategy: Strategy,
    wallet_private_key: str,
    signature_type: int,
    funder: str | None = None,
    risk_manager: RiskManager | None = None,
    duration: float | None = None,
    max_position_size: Decimal = Decimal('1000'),
    max_total_exposure: Decimal = Decimal('10000'),
) -> None:
    """
    Run live trading on Polymarket with real orders.

    Args:
        data_source: The Polymarket live data source
        strategy: The trading strategy to execute
        wallet_private_key: Private key for the trading wallet
        signature_type: Signature type for Polymarket API
        funder: Optional funder address for safe wallets
        risk_manager: Optional risk manager (defaults to StandardRiskManager)
        duration: Optional duration in seconds to run
        max_position_size: Maximum position size per trade
        max_total_exposure: Maximum total portfolio exposure
    """
    market_data = MarketDataManager()
    position_manager = PositionManager()

    if risk_manager is None:
        risk_manager = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_position_size=max_position_size,
            max_total_exposure=max_total_exposure,
            max_single_trade_size=max_position_size / Decimal('2'),
            max_drawdown_pct=Decimal('0.2'),
        )

    trader = PolymarketTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        wallet_private_key=wallet_private_key,
        signature_type=signature_type,
        funder=funder,
    )

    # Initialize USDC position from Polymarket balance
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    balance_info = trader.clob_client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    initial_balance = Decimal(balance_info['balance']) / Decimal('1000000')

    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_balance,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    print(f'Starting live Polymarket trading with balance: {initial_balance} USDC')

    await run_live_trading(data_source, strategy, trader, duration)

    # Print final portfolio status
    print('\n--- Final Portfolio Status ---')
    print(f'Cash positions: {position_manager.get_cash_positions()}')
    print(f'Non-cash positions: {position_manager.get_non_cash_positions()}')
    print(f'Total realized PnL: {position_manager.get_total_realized_pnl()}')


if __name__ == '__main__':
    from swm_agent.strategy.test_strategy import TestStrategy

    async def main():
        # Example: Run paper trading with RSS news data
        data_source = LiveRSSNewsDataSource(
            polling_interval=60.0,
            max_articles_per_poll=5,
        )

        await run_live_paper_trading(
            data_source=data_source,
            strategy=TestStrategy(),
            initial_capital=Decimal('10000'),
            duration=300,  # Run for 5 minutes
        )

    asyncio.run(main())
