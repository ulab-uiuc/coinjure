from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from coinjure.engine import runner as live_trader
from coinjure.ticker import CashTicker, PolyMarketTicker
from coinjure.trading.position import Position
from coinjure.trading.risk import NoRiskManager


@pytest.mark.asyncio
async def test_run_live_trading_passes_continuous_and_drawdown(monkeypatch):
    captured: dict = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            captured['started'] = True

        async def stop(self):
            captured['stopped'] = True

    monkeypatch.setattr(live_trader, 'TradingEngine', FakeEngine)

    await live_trader.run_live_trading(
        data_source=object(),
        strategy=object(),
        trader=object(),
        continuous=False,
        drawdown_alert_pct=Decimal('0.12'),
    )

    assert captured['continuous'] is False
    assert captured['drawdown_alert_pct'] == Decimal('0.12')
    assert captured['started'] is True


@pytest.mark.asyncio
async def test_run_live_polymarket_trading_fetches_positions_from_exchange(monkeypatch):
    """Live runner should fetch cash + token positions directly from exchange."""
    captured: dict = {}

    class FakePolymarketTrader:
        def __init__(self, **kwargs):
            self.position_manager = kwargs['position_manager']
            self.clob_client = self
            captured['trader'] = self

        def get_balance_allowance(self, params):
            if params.asset_type == 'conditional':
                # Return conditional token balance (6 decimals)
                return {'balance': '3000000'}  # 3 tokens
            return {'balance': '2500000'}  # 2.5 USDC

    async def fake_run_live_trading(*args, **kwargs):
        captured['run_called'] = True

    # Provide a minimal fake py_clob_client.clob_types module for function-local import.
    pkg = types.ModuleType('py_clob_client')
    sub = types.ModuleType('py_clob_client.clob_types')

    class AssetType:
        COLLATERAL = 'collateral'
        CONDITIONAL = 'conditional'

    class BalanceAllowanceParams:
        def __init__(self, asset_type, token_id=None):
            self.asset_type = asset_type
            self.token_id = token_id

    sub.AssetType = AssetType
    sub.BalanceAllowanceParams = BalanceAllowanceParams
    monkeypatch.setitem(sys.modules, 'py_clob_client', pkg)
    monkeypatch.setitem(sys.modules, 'py_clob_client.clob_types', sub)

    monkeypatch.setattr(live_trader, 'PolymarketTrader', FakePolymarketTrader)
    monkeypatch.setattr(live_trader, 'run_live_trading', fake_run_live_trading)

    class FakeStrategy:
        def watch_tokens(self):
            return ['tok1']

    await live_trader.run_live_polymarket_trading(
        data_source=object(),
        strategy=FakeStrategy(),
        wallet_private_key='dummy',
        signature_type=0,
        risk_manager=NoRiskManager(),
    )

    pm = captured['trader'].position_manager

    # Cash fetched from exchange
    cash = pm.get_position(CashTicker.POLYMARKET_USDC)
    assert cash is not None
    assert cash.quantity == Decimal('2.5')

    # Token position fetched from exchange
    token_pos = pm.get_position(PolyMarketTicker(symbol='tok1', token_id='tok1'))
    assert token_pos is not None
    assert token_pos.quantity == Decimal('3')

    assert captured['run_called'] is True
