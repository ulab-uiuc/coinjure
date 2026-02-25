"""Agent-first CLI commands for strategy, backtest, paper, and live modes."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import click

from swm_agent.backtest.backtester import run_backtest
from swm_agent.data.live.kalshi_data_source import LiveKalshiDataSource
from swm_agent.data.live.live_data_source import (
    LivePolyMarketDataSource,
    LiveRSSNewsDataSource,
)
from swm_agent.live.live_trader import (
    run_live_kalshi_paper_trading,
    run_live_kalshi_trading,
    run_live_paper_trading,
    run_live_polymarket_trading,
)
from swm_agent.strategy.strategy import Strategy
from swm_agent.ticker.ticker import PolyMarketTicker


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload))
    else:
        click.echo(payload.get('message', str(payload)))


def _load_strategy_class(strategy_ref: str) -> type[Strategy]:
    if ':' not in strategy_ref:
        raise click.ClickException(
            "Invalid strategy reference. Use 'module.path:ClassName' or '/path/to/file.py:ClassName'."
        )

    module_or_file, class_name = strategy_ref.split(':', 1)

    if module_or_file.endswith('.py') or os.path.sep in module_or_file:
        file_path = Path(module_or_file).expanduser().resolve()
        if not file_path.exists():
            raise click.ClickException(f'Strategy file not found: {file_path}')
        module_name = f'_swm_user_strategy_{uuid.uuid4().hex}'
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise click.ClickException(f'Could not load strategy file: {file_path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_or_file)

    strategy_cls = getattr(module, class_name, None)
    if strategy_cls is None:
        raise click.ClickException(
            f'Class {class_name!r} not found in {module_or_file!r}'
        )
    if not isinstance(strategy_cls, type) or not issubclass(strategy_cls, Strategy):
        raise click.ClickException(
            f'Class {class_name!r} must inherit from swm_agent.strategy.strategy.Strategy'
        )
    return strategy_cls


def _load_strategy(strategy_ref: str) -> Strategy:
    strategy_cls = _load_strategy_class(strategy_ref)
    try:
        return strategy_cls()
    except TypeError as exc:
        raise click.ClickException(
            f'Could not instantiate strategy {strategy_ref!r} with zero arguments: {exc}'
        ) from exc


@click.group()
def strategy() -> None:
    """Strategy development commands."""


@strategy.command('create')
@click.option('--output', 'output_path', required=True, type=click.Path(path_type=Path))
@click.option('--class-name', default='AgentStrategy', show_default=True)
@click.option(
    '--force', is_flag=True, default=False, help='Overwrite output file if it exists.'
)
def strategy_create(output_path: Path, class_name: str, force: bool) -> None:
    """Create a strategy template that agents can edit."""
    if output_path.exists() and not force:
        raise click.ClickException(
            f'File already exists: {output_path}. Pass --force to overwrite.'
        )

    template = f"""from decimal import Decimal

from swm_agent.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from swm_agent.strategy.strategy import Strategy
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import TradeSide


class {class_name}(Strategy):
    \"\"\"Template strategy for agent-driven development.

    Implement your signal logic in ``process_event`` and call:
      await trader.place_order(side=..., ticker=..., limit_price=..., quantity=...)
    \"\"\"

    def __init__(self) -> None:
        self.trade_size = Decimal('10')

    async def process_event(self, event: Event, trader: Trader) -> None:
        # Example skeleton:
        if isinstance(event, NewsEvent):
            # Analyze event.title / event.news and decide.
            return
        if isinstance(event, PriceChangeEvent):
            # Use event.price dynamics for momentum/reversion logic.
            return
        if isinstance(event, OrderBookEvent):
            # Use bid/ask updates for microstructure-aware execution.
            return
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template)
    click.echo(f'Created strategy template: {output_path}')


@strategy.command('validate')
@click.option(
    '--strategy-ref',
    required=True,
    help='Strategy ref: module:Class or /path/file.py:Class',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_validate(strategy_ref: str, as_json: bool) -> None:
    """Validate that a strategy is importable and constructible."""
    strategy_obj = _load_strategy(strategy_ref)
    payload = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'class': strategy_obj.__class__.__name__,
        'module': strategy_obj.__class__.__module__,
        'message': f'Valid strategy: {strategy_ref}',
    }
    _emit(payload, as_json=as_json)


@click.group()
def backtest() -> None:
    """Backtest commands."""


@backtest.command('run')
@click.option(
    '--history-file', required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option('--symbol', default='BACKTEST_TOKEN', show_default=True)
@click.option('--name', default='Backtest Market', show_default=True)
@click.option('--market-id', required=True)
@click.option('--event-id', required=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default='swm_agent.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def backtest_run(
    history_file: str,
    symbol: str,
    name: str,
    market_id: str,
    event_id: str,
    initial_capital: str,
    strategy_ref: str,
    as_json: bool,
) -> None:
    """Run backtest mode with historical data + paper execution."""
    strategy_obj = _load_strategy(strategy_ref)
    ticker = PolyMarketTicker(
        symbol=symbol,
        name=name,
        market_id=market_id,
        event_id=event_id,
        token_id=symbol,
    )
    capital = Decimal(initial_capital)
    _emit(
        {
            'mode': 'backtest',
            'message': f'Starting backtest: {strategy_ref}',
            'history_file': history_file,
            'symbol': symbol,
        },
        as_json=as_json,
    )
    asyncio.run(
        run_backtest(
            history_file=history_file,
            ticker_symbol=ticker,
            initial_capital=capital,
            strategy=strategy_obj,
        )
    )
    _emit({'mode': 'backtest', 'message': 'Backtest completed'}, as_json=as_json)


@click.group()
def paper() -> None:
    """Paper trading commands (live data + simulated execution)."""


@paper.command('run')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'rss']),
    default='polymarket',
)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--strategy-ref',
    default='swm_agent.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def paper_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str,
    as_json: bool,
) -> None:
    """Run paper trading in simulation mode."""
    strategy_obj = _load_strategy(strategy_ref)
    capital = Decimal(initial_capital)
    _emit(
        {
            'mode': 'paper',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'message': f'Starting paper mode ({exchange})',
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        data_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
            )
        )
    elif exchange == 'kalshi':
        data_source = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_kalshi_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
            )
        )
    else:
        data_source = LiveRSSNewsDataSource(
            polling_interval=60.0,
            max_articles_per_poll=5,
        )
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
            )
        )

    _emit({'mode': 'paper', 'message': 'Paper session ended'}, as_json=as_json)


@click.group()
def live() -> None:
    """Live trading commands (real exchange execution)."""


@live.command('run')
@click.option('--exchange', type=click.Choice(['polymarket', 'kalshi']), required=True)
@click.option(
    '--duration', type=float, default=None, help='Seconds to run (default: forever)'
)
@click.option(
    '--strategy-ref',
    default='swm_agent.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--wallet-private-key',
    default=None,
    help='Polymarket wallet private key (or POLYMARKET_PRIVATE_KEY)',
)
@click.option('--signature-type', default=0, show_default=True, type=int)
@click.option('--funder', default=None, help='Polymarket funder wallet (optional)')
@click.option(
    '--kalshi-api-key-id', default=None, help='Kalshi API key id (or KALSHI_API_KEY_ID)'
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH)',
)
def live_run(
    exchange: str,
    duration: float | None,
    strategy_ref: str,
    as_json: bool,
    wallet_private_key: str | None,
    signature_type: int,
    funder: str | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
) -> None:
    """Run live mode with real order placement."""
    strategy_obj = _load_strategy(strategy_ref)
    _emit(
        {
            'mode': 'live',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'message': f'Starting live mode ({exchange})',
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        private_key = wallet_private_key or os.environ.get('POLYMARKET_PRIVATE_KEY')
        if not private_key:
            raise click.ClickException(
                'Missing Polymarket key. Pass --wallet-private-key or set POLYMARKET_PRIVATE_KEY.'
            )
        data_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_polymarket_trading(
                data_source=data_source,
                strategy=strategy_obj,
                wallet_private_key=private_key,
                signature_type=signature_type,
                funder=funder,
                duration=duration,
                continuous=True,
            )
        )
    else:
        data_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_kalshi_trading(
                data_source=data_source,
                strategy=strategy_obj,
                api_key_id=kalshi_api_key_id,
                private_key_path=kalshi_private_key_path,
                duration=duration,
            )
        )

    _emit({'mode': 'live', 'message': 'Live session ended'}, as_json=as_json)
