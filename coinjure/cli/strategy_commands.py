"""Strategy development & testing CLI group.

Commands
--------
  strategy validate    — validate strategy loads + dry-run
  strategy backtest    — parquet orderbook backtest
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import click

from coinjure.cli.agent_commands import (
    _build_mock_events,
    _load_strategy,
    _parse_strategy_kwargs_json,
)
from coinjure.cli.utils import _emit
from coinjure.events import OrderBookEvent, PriceChangeEvent
from coinjure.ticker import PolyMarketTicker


@click.group()
def strategy() -> None:
    """Strategy development & testing."""


# ── validate ──────────────────────────────────────────────────────────────────


@strategy.command('validate')
@click.option(
    '--strategy-ref',
    required=True,
    help='Strategy ref: module:Class or /path/file.py:Class',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option(
    '--dry-run/--no-dry-run',
    default=True,
    show_default=True,
    help='Feed synthetic events to test process_event.',
)
@click.option('--events', default=10, show_default=True, type=int)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def strategy_validate(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    dry_run: bool,
    events: int,
    as_json: bool,
) -> None:
    """Validate strategy: import, instantiate, and optionally dry-run."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    payload: dict[str, object] = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'strategy_class': type(strategy_obj).__name__,
        'strategy_name': getattr(strategy_obj, 'name', None),
        'strategy_version': getattr(strategy_obj, 'version', None),
        'strategy_kwargs': strategy_kwargs,
    }
    if not dry_run:
        _emit(payload, as_json=as_json)
        return

    from coinjure.data.data_manager import DataManager
    from coinjure.engine.trader.paper_trader import PaperTrader
    from coinjure.engine.trader.position_manager import Position, PositionManager
    from coinjure.engine.trader.risk_manager import NoRiskManager
    from coinjure.ticker import CashTicker

    ticker = PolyMarketTicker(
        symbol='VALIDATE_TOKEN',
        name='Validation Market',
        market_id='validate_market',
    )
    market_data = DataManager(
        spread=Decimal('0.01'),
        max_history_per_ticker=None,
        max_timeline_events=None,
        synthetic_book=True,
    )
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )
    trader = PaperTrader(
        market_data=market_data,
        risk_manager=NoRiskManager(),
        position_manager=position_manager,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    error_message = ''
    orders_created = 0
    event_stream = _build_mock_events(ticker, events)
    loop = asyncio.new_event_loop()
    try:
        for event in event_stream:
            try:
                if isinstance(event, OrderBookEvent):
                    market_data.process_orderbook_event(event)
                elif isinstance(event, PriceChangeEvent):
                    market_data.process_price_change_event(event)

                loop.run_until_complete(strategy_obj.process_event(event, trader))
                orders_created = len(trader.orders)
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                break
    finally:
        loop.close()

    decision_stats = strategy_obj.get_decision_stats()
    payload.update(
        {
            'dry_run': True,
            'events_requested': events,
            'events_processed': len(event_stream),
            'orders_created': orders_created,
            'decision_stats': decision_stats,
            'decisions': [
                {
                    'ticker': d.ticker_name,
                    'action': d.action,
                    'executed': d.executed,
                    'reasoning': d.reasoning,
                }
                for d in list(strategy_obj.get_decisions())[-5:]
            ],
            'error': error_message or None,
            'message': 'Dry-run completed' if error_message == '' else 'Dry-run failed',
        }
    )
    _emit(payload, as_json=as_json)
    if error_message:
        raise click.ClickException(f'Dry-run failed: {error_message}')
    return

    _emit(payload, as_json=as_json)


# ── backtest ──────────────────────────────────────────────────────────────────


@strategy.command('backtest')
@click.option(
    '--parquet',
    'parquet_paths',
    required=True,
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help='Parquet orderbook file(s). Repeat for multi-hour backtest.',
)
@click.option(
    '--market-id',
    multiple=True,
    help='Filter to specific market(s). Repeat for multi-market.',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default='coinjure.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def strategy_backtest(
    parquet_paths: tuple[str, ...],
    market_id: tuple[str, ...],
    initial_capital: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
) -> None:
    """Run backtest with parquet orderbook data + paper execution."""
    from coinjure.engine.backtester import run_backtest_parquet

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    capital = Decimal(initial_capital)
    market_ids = list(market_id) if market_id else None
    paths = list(parquet_paths) if len(parquet_paths) > 1 else parquet_paths[0]
    _emit(
        {
            'mode': 'backtest',
            'message': f'Starting backtest: {strategy_ref}',
            'parquet_files': len(parquet_paths),
            'market_ids': market_ids,
            'strategy_kwargs': strategy_kwargs,
        },
        as_json=as_json,
    )
    asyncio.run(
        run_backtest_parquet(
            parquet_path=paths,
            initial_capital=capital,
            strategy=strategy_obj,
            market_ids=market_ids,
        )
    )
    _emit({'mode': 'backtest', 'message': 'Backtest completed'}, as_json=as_json)
