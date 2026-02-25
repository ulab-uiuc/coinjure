from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from swm_agent.core.trading_engine import TradingEngine
from swm_agent.data.live.kalshi_data_source import LiveKalshiDataSource
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
from swm_agent.trader.kalshi_trader import KalshiTrader
from swm_agent.trader.paper_trader import PaperTrader
from swm_agent.trader.polymarket_trader import PolymarketTrader
from swm_agent.trader.trader import Trader

if TYPE_CHECKING:
    from swm_agent.alerts.alerter import Alerter
    from swm_agent.storage.state_store import StateStore

logger = logging.getLogger(__name__)


async def run_live_trading(
    data_source: LivePolyMarketDataSource
    | LiveNewsDataSource
    | LiveRSSNewsDataSource
    | LiveKalshiDataSource,
    strategy: Strategy,
    trader: Trader,
    duration: float | None = None,
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
) -> None:
    """
    Run live trading with the given data source, strategy, and trader.

    Args:
        data_source: The live data source to use (Polymarket, News API, or RSS)
        strategy: The trading strategy to execute
        trader: The trader implementation (Paper or Polymarket)
        duration: Optional duration in seconds to run. If None, runs indefinitely.
        state_store: Optional state store for persistence.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
    """
    engine = TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        state_store=state_store,
        alerter=alerter,
        continuous=continuous,
        drawdown_alert_pct=drawdown_alert_pct,
    )

    # NOTE: data_source.start() is called by engine.start() internally
    # (guarded by _ds_started flag). Do NOT call it here to avoid double-starting.

    if duration:
        # Run for specified duration
        try:
            await asyncio.wait_for(engine.start(), timeout=duration)
        except asyncio.TimeoutError:
            await engine.stop()
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
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
) -> None:
    """
    Run live paper trading (simulated) with the given configuration.

    Args:
        data_source: The live data source to use
        strategy: The trading strategy to execute
        initial_capital: Starting capital in USDC
        risk_manager: Optional risk manager (defaults to NoRiskManager)
        duration: Optional duration in seconds to run
        state_store: Optional state store for persistence and state recovery.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
    """
    market_data = MarketDataManager()
    position_manager = PositionManager()

    # State recovery: load saved positions if available.
    saved_positions = state_store.load_positions() if state_store else []
    if saved_positions:
        logger.info('Restoring %d positions from state store', len(saved_positions))
        for pos in saved_positions:
            position_manager.update_position(pos)
    else:
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
        alerter=alerter,
    )

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        state_store,
        alerter,
        continuous,
        drawdown_alert_pct,
    )

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
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
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
        state_store: Optional state store for persistence and state recovery.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
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
        alerter=alerter,
    )

    # Initialize USDC position: prefer live exchange balance for reconciliation.
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    balance_info = trader.clob_client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    live_balance = Decimal(balance_info['balance']) / Decimal('1000000')

    # State recovery: load saved positions if available, then reconcile cash.
    saved_positions = state_store.load_positions() if state_store else []
    if saved_positions:
        logger.info('Restoring %d positions from state store', len(saved_positions))
        for pos in saved_positions:
            position_manager.update_position(pos)
        # Reconcile cash position against live exchange balance.
        saved_cash = position_manager.get_position(CashTicker.POLYMARKET_USDC)
        if saved_cash is not None:
            diff_pct = abs(live_balance - saved_cash.quantity) / max(
                live_balance, Decimal('1')
            )
            if diff_pct > Decimal('0.01'):
                logger.warning(
                    'Saved cash %.4f differs from live balance %.4f by %.1f%% — using live value',
                    saved_cash.quantity,
                    live_balance,
                    float(diff_pct) * 100,
                )
                position_manager.update_position(
                    Position(
                        ticker=CashTicker.POLYMARKET_USDC,
                        quantity=live_balance,
                        average_cost=Decimal('0'),
                        realized_pnl=saved_cash.realized_pnl,
                    )
                )
        else:
            position_manager.update_position(
                Position(
                    ticker=CashTicker.POLYMARKET_USDC,
                    quantity=live_balance,
                    average_cost=Decimal('0'),
                    realized_pnl=Decimal('0'),
                )
            )
    else:
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=live_balance,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

    print(f'Starting live Polymarket trading with balance: {live_balance} USDC')

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        state_store,
        alerter,
        continuous,
        drawdown_alert_pct,
    )

    # Print final portfolio status
    print('\n--- Final Portfolio Status ---')
    print(f'Cash positions: {position_manager.get_cash_positions()}')
    print(f'Non-cash positions: {position_manager.get_non_cash_positions()}')
    print(f'Total realized PnL: {position_manager.get_total_realized_pnl()}')


async def run_live_kalshi_paper_trading(
    data_source: LiveKalshiDataSource,
    strategy: Strategy,
    initial_capital: Decimal,
    risk_manager: RiskManager | None = None,
    duration: float | None = None,
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
) -> None:
    """
    Run live paper trading on Kalshi markets (simulated).

    Args:
        data_source: The Kalshi live data source
        strategy: The trading strategy to execute
        initial_capital: Starting capital in USD
        risk_manager: Optional risk manager (defaults to NoRiskManager)
        duration: Optional duration in seconds to run
        state_store: Optional state store for persistence and state recovery.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
    """
    market_data = MarketDataManager()
    position_manager = PositionManager()

    saved_positions = state_store.load_positions() if state_store else []
    if saved_positions:
        logger.info('Restoring %d positions from state store', len(saved_positions))
        for pos in saved_positions:
            position_manager.update_position(pos)
    else:
        position_manager.update_position(
            Position(
                ticker=CashTicker.KALSHI_USD,
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
        alerter=alerter,
    )

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        state_store,
        alerter,
        continuous,
        drawdown_alert_pct,
    )

    print('\n--- Final Portfolio Status ---')
    print(f'Cash positions: {position_manager.get_cash_positions()}')
    print(f'Non-cash positions: {position_manager.get_non_cash_positions()}')
    print(f'Total realized PnL: {position_manager.get_total_realized_pnl()}')


async def run_live_kalshi_trading(
    data_source: LiveKalshiDataSource,
    strategy: Strategy,
    api_key_id: str | None = None,
    private_key_path: str | None = None,
    risk_manager: RiskManager | None = None,
    duration: float | None = None,
    max_position_size: Decimal = Decimal('1000'),
    max_total_exposure: Decimal = Decimal('10000'),
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
) -> None:
    """
    Run live trading on Kalshi with real orders.

    Args:
        data_source: The Kalshi live data source
        strategy: The trading strategy to execute
        api_key_id: Kalshi API key ID (or set KALSHI_API_KEY_ID env)
        private_key_path: Path to RSA private key PEM file (or set KALSHI_PRIVATE_KEY_PATH env)
        risk_manager: Optional risk manager (defaults to StandardRiskManager)
        duration: Optional duration in seconds to run
        max_position_size: Maximum position size per trade
        max_total_exposure: Maximum total portfolio exposure
        state_store: Optional state store for persistence and state recovery.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
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

    trader = KalshiTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        alerter=alerter,
    )

    # Fetch initial balance from Kalshi (via the trader's portfolio API)
    balance_response = await asyncio.to_thread(
        lambda: trader._portfolio_api.get_balance()
    )
    # Balance is in cents
    initial_balance = Decimal(str(balance_response.balance)) / Decimal('100')

    saved_positions = state_store.load_positions() if state_store else []
    if saved_positions:
        logger.info('Restoring %d positions from state store', len(saved_positions))
        for pos in saved_positions:
            position_manager.update_position(pos)
        saved_cash = position_manager.get_position(CashTicker.KALSHI_USD)
        if saved_cash is not None:
            diff_pct = abs(initial_balance - saved_cash.quantity) / max(
                initial_balance, Decimal('1')
            )
            if diff_pct > Decimal('0.01'):
                logger.warning(
                    'Saved cash %.2f differs from live balance %.2f by %.1f%% — using live value',
                    saved_cash.quantity,
                    initial_balance,
                    float(diff_pct) * 100,
                )
                position_manager.update_position(
                    Position(
                        ticker=CashTicker.KALSHI_USD,
                        quantity=initial_balance,
                        average_cost=Decimal('0'),
                        realized_pnl=saved_cash.realized_pnl,
                    )
                )
        else:
            position_manager.update_position(
                Position(
                    ticker=CashTicker.KALSHI_USD,
                    quantity=initial_balance,
                    average_cost=Decimal('0'),
                    realized_pnl=Decimal('0'),
                )
            )
    else:
        position_manager.update_position(
            Position(
                ticker=CashTicker.KALSHI_USD,
                quantity=initial_balance,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

    logger.info('Starting live Kalshi trading with balance: $%s', initial_balance)

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        state_store,
        alerter,
        continuous,
        drawdown_alert_pct,
    )

    logger.info('--- Final Portfolio Status ---')
    logger.info('Cash positions: %s', position_manager.get_cash_positions())
    logger.info('Non-cash positions: %s', position_manager.get_non_cash_positions())
    logger.info('Total realized PnL: %s', position_manager.get_total_realized_pnl())


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
