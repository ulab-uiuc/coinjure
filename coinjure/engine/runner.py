from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from coinjure.data.live.kalshi import LiveKalshiDataSource
from coinjure.data.live.polymarket import LivePolyMarketDataSource
from coinjure.data.manager import DataManager
from coinjure.data.source import DataSource
from coinjure.engine.engine import TradingEngine
from coinjure.engine.trader.kalshi import KalshiTrader
from coinjure.engine.trader.paper import PaperTrader
from coinjure.engine.trader.polymarket import PolymarketTrader
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import CashTicker
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import (
    RiskManager,
    StandardRiskManager,
)
from coinjure.trading.trader import Trader

if TYPE_CHECKING:
    from coinjure.engine.trader.alerter import Alerter
    from coinjure.storage.state_store import StateStore

logger = logging.getLogger(__name__)


def _emit_stdout(message: str, *, emit_text: bool) -> None:
    if emit_text:
        print(message)


def _fund_position(
    pm: PositionManager,
    ticker: CashTicker,
    amount: Decimal,
) -> None:
    """Add initial cash position for a single currency."""
    pm.update_position(
        Position(
            ticker=ticker,
            quantity=amount,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )


async def run_live_trading(
    data_source: DataSource,
    strategy: Strategy,
    trader: Trader,
    duration: float | None = None,
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
    monitor: bool = False,
    exchange_name: str = '',
    emit_text: bool = True,
    socket_path: Path | None = None,
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
        monitor: Enable the Textual TUI dashboard and ControlServer.
        exchange_name: Exchange name shown in the monitor header.
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

    from coinjure.cli.utils import add_monitoring_to_engine

    monitored = add_monitoring_to_engine(
        engine,
        watch=monitor,
        exchange_name=exchange_name,
        socket_path=socket_path,
    )
    if duration:
        try:
            await asyncio.wait_for(monitored.start(), timeout=duration)
        except asyncio.TimeoutError:
            await monitored.stop()
            _emit_stdout(
                f'Live trading stopped after {duration} seconds.',
                emit_text=emit_text,
            )
    else:
        await monitored.start()

    _emit_stdout('Live trading session ended.', emit_text=emit_text)


async def run_live_paper_trading(
    data_source: DataSource,
    strategy: Strategy,
    initial_capital: Decimal,
    risk_manager: RiskManager | None = None,
    duration: float | None = None,
    state_store: StateStore | None = None,
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
    monitor: bool = False,
    exchange_name: str = '',
    emit_text: bool = True,
    socket_path: Path | None = None,
) -> None:
    """
    Run live paper trading (simulated) with the given configuration.

    Funds both Polymarket USDC and Kalshi USD so cross-platform arbs work.

    Args:
        data_source: The live data source to use
        strategy: The trading strategy to execute
        initial_capital: Starting capital per currency
        risk_manager: Optional risk manager (defaults to StandardRiskManager)
        duration: Optional duration in seconds to run
        state_store: Optional state store for persistence and state recovery.
        alerter: Optional alerter for notifications.
        continuous: Keep engine running when the data source is temporarily idle.
        drawdown_alert_pct: Optional drawdown alert threshold as a decimal (0.1 = 10%).
    """
    market_data = DataManager()
    position_manager = PositionManager()

    # State recovery: load saved positions if available.
    saved_positions = state_store.load_positions() if state_store else []
    if saved_positions:
        logger.info('Restoring %d positions from state store', len(saved_positions))
        for pos in saved_positions:
            position_manager.update_position(pos)
    else:
        _fund_position(position_manager, CashTicker.POLYMARKET_USDC, initial_capital)
        _fund_position(position_manager, CashTicker.KALSHI_USD, initial_capital)

    if risk_manager is None:
        risk_manager = StandardRiskManager(
            position_manager=position_manager,
            market_data=market_data,
            max_position_size=initial_capital,
            max_total_exposure=initial_capital * Decimal('2'),
            max_single_trade_size=initial_capital / Decimal('2'),
            max_drawdown_pct=Decimal('0.2'),
        )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),  # match live trader defaults
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
        monitor=monitor,
        exchange_name=exchange_name,
        emit_text=emit_text,
        socket_path=socket_path,
    )

    # Print final portfolio status
    _emit_stdout('\n--- Final Portfolio Status ---', emit_text=emit_text)
    _emit_stdout(
        f'Cash positions: {position_manager.get_cash_positions()}',
        emit_text=emit_text,
    )
    _emit_stdout(
        f'Non-cash positions: {position_manager.get_non_cash_positions()}',
        emit_text=emit_text,
    )
    _emit_stdout(
        f'Total realized PnL: {position_manager.get_total_realized_pnl()}',
        emit_text=emit_text,
    )


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
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
    monitor: bool = False,
    exchange_name: str = '',
    emit_text: bool = True,
) -> None:
    """
    Run live trading on Polymarket with real orders.

    Positions are fetched directly from the exchange on startup — no local
    state file is needed.
    """
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    market_data = DataManager()
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

    # ── Fetch USDC balance from exchange ────────────────────────────────
    balance_info = trader.clob_client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    live_balance = Decimal(balance_info['balance']) / Decimal('1000000')
    _fund_position(position_manager, CashTicker.POLYMARKET_USDC, live_balance)

    # ── Fetch conditional token positions from exchange ─────────────────
    for token_id in strategy.watch_tokens():
        try:
            bal_info = trader.clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            qty = Decimal(bal_info['balance']) / Decimal('1000000')
            if qty > 0:
                from coinjure.ticker import PolyMarketTicker

                ticker = PolyMarketTicker(
                    symbol=token_id,
                    name='',
                    token_id=token_id,
                )
                position_manager.update_position(
                    Position(
                        ticker=ticker,
                        quantity=qty,
                        average_cost=Decimal('0'),
                        realized_pnl=Decimal('0'),
                    )
                )
                logger.info(
                    'Loaded position from exchange: %s qty=%s',
                    token_id,
                    qty,
                )
        except Exception:
            logger.warning(
                'Failed to fetch conditional balance for token %s',
                token_id,
                exc_info=True,
            )

    print(f'Starting live Polymarket trading with balance: {live_balance} USDC')

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        None,  # no state_store for live trading
        alerter,
        continuous,
        drawdown_alert_pct,
        monitor=monitor,
        exchange_name=exchange_name,
    )

    # Print final portfolio status
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
    alerter: Alerter | None = None,
    continuous: bool = True,
    drawdown_alert_pct: Decimal | None = None,
    monitor: bool = False,
    exchange_name: str = '',
) -> None:
    """
    Run live trading on Kalshi with real orders.

    Positions are fetched directly from the exchange on startup — no local
    state file is needed.
    """
    market_data = DataManager()
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

    # ── Fetch USD balance from exchange ─────────────────────────────────
    balance_response = await asyncio.to_thread(
        lambda: trader._portfolio_api.get_balance()
    )
    initial_balance = Decimal(str(balance_response.balance)) / Decimal('100')
    _fund_position(position_manager, CashTicker.KALSHI_USD, initial_balance)

    # ── Fetch contract positions from exchange ──────────────────────────
    try:
        positions_response = await asyncio.to_thread(
            lambda: trader._portfolio_api.get_positions(limit=200)
        )
        for kpos in positions_response.positions or []:
            count = kpos.position or 0
            if count == 0 or not kpos.ticker:
                continue
            from coinjure.ticker import KalshiTicker

            # Kalshi: positive count = YES, negative = NO
            if count > 0:
                side = 'yes'
                abs_count = count
            else:
                side = 'no'
                abs_count = abs(count)

            # Use distinct symbol for YES vs NO so PositionManager doesn't overwrite
            sym = kpos.ticker if side == 'yes' else f'{kpos.ticker}__no'
            ticker = KalshiTicker(
                symbol=sym,
                name='',
                market_ticker=kpos.ticker,
                event_ticker=kpos.event_ticker or '',
                side=side,
            )
            avg_cost = Decimal('0')
            if kpos.total_cost is not None and abs_count > 0:
                # total_cost is in cents
                avg_cost = (
                    Decimal(str(abs(kpos.total_cost))) / Decimal('100') / Decimal(str(abs_count))
                )
            position_manager.update_position(
                Position(
                    ticker=ticker,
                    quantity=Decimal(str(abs_count)),
                    average_cost=avg_cost,
                    realized_pnl=Decimal(str(kpos.realized_pnl or 0)) / Decimal('100'),
                )
            )
            logger.info(
                'Loaded position from exchange: %s side=%s qty=%d',
                kpos.ticker,
                side,
                abs_count,
            )
    except Exception:
        logger.warning('Failed to fetch positions from Kalshi', exc_info=True)

    logger.info('Starting live Kalshi trading with balance: $%s', initial_balance)

    await run_live_trading(
        data_source,
        strategy,
        trader,
        duration,
        None,  # no state_store for live trading
        alerter,
        continuous,
        drawdown_alert_pct,
        monitor=monitor,
        exchange_name=exchange_name,
    )

    logger.info('--- Final Portfolio Status ---')
    logger.info('Cash positions: %s', position_manager.get_cash_positions())
    logger.info('Non-cash positions: %s', position_manager.get_non_cash_positions())
    logger.info('Total realized PnL: %s', position_manager.get_total_realized_pnl())
