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
from typing import Any

import click

from coinjure.backtest.backtester import run_backtest
from coinjure.data.composite_data_source import CompositeDataSource
from coinjure.data.live.google_news_data_source import GoogleNewsDataSource
from coinjure.data.live.kalshi_data_source import LiveKalshiDataSource
from coinjure.data.live.live_data_source import (
    LivePolyMarketDataSource,
    LiveRSSNewsDataSource,
)
from coinjure.events.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.live.live_trader import (
    run_live_kalshi_paper_trading,
    run_live_kalshi_trading,
    run_live_paper_trading,
    run_live_polymarket_trading,
)
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import PolyMarketTicker
from coinjure.trader.trader import Trader


class _IdleStrategy(Strategy):
    """No-op strategy: consume events without placing orders."""

    async def process_event(self, event: Event, trader: Trader) -> None:
        return


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(payload.get('message', str(payload)))


def _parse_strategy_kwargs_json(strategy_kwargs_json: str | None) -> dict[str, Any]:
    if not strategy_kwargs_json:
        return {}
    try:
        parsed = json.loads(strategy_kwargs_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f'Invalid --strategy-kwargs-json: {exc.msg}'
        ) from exc
    if not isinstance(parsed, dict):
        raise click.ClickException('--strategy-kwargs-json must be a JSON object.')
    return parsed


def _confirm_live_trading(*, as_json: bool) -> None:
    """Require explicit user confirmation before starting live trading."""
    disclaimer = (
        'DISCLAIMER: Live trading places real orders with real funds. '
        'You are fully responsible for all losses, fees, and operational risk.'
    )

    if as_json:
        raise click.ClickException(
            'Live trading confirmation required in interactive mode.'
        )

    click.echo(click.style(disclaimer, fg='yellow'))
    confirmed = click.confirm(
        'Proceed with live trading?',
        default=True,
        show_default=True,
    )
    if not confirmed:
        raise click.ClickException('Live trading cancelled by user.')


def _build_news_augmented_source(exchange: str):
    """Build market + Google/RSS news composite for paper trading."""
    if exchange == 'polymarket':
        market_source = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
    elif exchange == 'kalshi':
        market_source = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
    else:
        raise click.ClickException(f'Unsupported exchange for market feed: {exchange}')

    # Keep Google polling conservative to reduce block/rate-limit risk.
    google_source = GoogleNewsDataSource(
        queries=[
            'polymarket prediction market',
            'kalshi prediction market',
            'US politics elections 2026',
            'federal reserve inflation jobs report',
            'geopolitics world events',
            'crypto regulation SEC CFTC',
        ],
        cache_file='google_news_cache.jsonl',
        polling_interval=600.0,
        max_articles_per_poll=8,
        max_pages=1,
        min_delay=3.0,
        max_delay=8.0,
    )
    rss_source = LiveRSSNewsDataSource(
        cache_file='rss_news_cache.jsonl',
        polling_interval=600.0,
        max_articles_per_poll=8,
        categories=['world', 'business', 'finance', 'politics', 'economy', 'sports'],
    )
    return CompositeDataSource([market_source, google_source, rss_source])


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
            f'Class {class_name!r} must inherit from coinjure.strategy.strategy.Strategy'
        )
    return strategy_cls


def _load_strategy(
    strategy_ref: str, strategy_kwargs: dict[str, Any] | None = None
) -> Strategy:
    kwargs = strategy_kwargs or {}
    strategy_cls = _load_strategy_class(strategy_ref)
    try:
        return strategy_cls(**kwargs)
    except TypeError as exc:
        raise click.ClickException(
            f'Could not instantiate strategy {strategy_ref!r} with kwargs={kwargs}: {exc}'
        ) from exc


def _build_mock_events(ticker: PolyMarketTicker, n_events: int) -> list[Event]:
    prices = [
        Decimal('0.47'),
        Decimal('0.49'),
        Decimal('0.46'),
        Decimal('0.51'),
        Decimal('0.48'),
    ]
    events: list[Event] = []
    for i in range(max(1, n_events)):
        base = prices[i % len(prices)]
        if i % 2 == 0:
            events.append(
                PriceChangeEvent(
                    ticker=ticker,
                    price=base,
                    timestamp=i + 1,
                )
            )
            continue

        side = 'bid' if i % 4 == 1 else 'ask'
        price = base - Decimal('0.01') if side == 'bid' else base + Decimal('0.01')
        size = Decimal('100') + Decimal(i * 10)
        events.append(
            OrderBookEvent(
                ticker=ticker,
                price=price,
                size=size,
                size_delta=Decimal('10'),
                side=side,
            )
        )
    return events


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

from coinjure.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.trader.trader import Trader
from coinjure.trader.types import TradeSide


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
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_validate(
    strategy_ref: str, strategy_kwargs_json: str | None, as_json: bool
) -> None:
    """Validate that a strategy is importable and constructible."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    payload = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'strategy_kwargs': strategy_kwargs,
        'class': strategy_obj.__class__.__name__,
        'module': strategy_obj.__class__.__module__,
        'message': f'Valid strategy: {strategy_ref}',
    }
    _emit(payload, as_json=as_json)


@strategy.command('dry-run')
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
    '--events',
    default=8,
    show_default=True,
    type=click.IntRange(1, 50),
    help='Number of mock events to feed.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_dry_run(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    events: int,
    as_json: bool,
) -> None:
    """Quickly validate strategy runtime behavior on mock events."""
    from coinjure.data.market_data_manager import MarketDataManager
    from coinjure.position.position_manager import Position, PositionManager
    from coinjure.risk.risk_manager import NoRiskManager
    from coinjure.ticker.ticker import CashTicker
    from coinjure.trader.paper_trader import PaperTrader

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    ticker = PolyMarketTicker(
        symbol='DRYRUN_YES',
        name='Dry Run Market',
        token_id='DRYRUN_YES',
        market_id='DRYRUN_MKT',
        event_id='DRYRUN_EVT',
        no_token_id='DRYRUN_NO',
    )

    market_data = MarketDataManager()
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
        min_fill_rate=Decimal('1.0'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    event_stream = _build_mock_events(ticker, events)

    async def _run_stream() -> tuple[int, str]:
        processed = 0
        error_message = ''
        for event in event_stream:
            if isinstance(event, OrderBookEvent):
                market_data.process_orderbook_event(event)
            elif isinstance(event, PriceChangeEvent):
                market_data.process_price_change_event(event)
            try:
                await strategy_obj.process_event(event, trader)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                break
        return processed, error_message

    processed, error_message = asyncio.run(_run_stream())

    decision_stats = strategy_obj.get_decision_stats()
    decisions = strategy_obj.get_decisions()
    payload = {
        'ok': error_message == '',
        'strategy_ref': strategy_ref,
        'strategy_kwargs': strategy_kwargs,
        'events_requested': events,
        'events_processed': processed,
        'orders_created': len(trader.orders),
        'decision_stats': decision_stats,
        'decisions_sample': [
            {
                'timestamp': d.timestamp,
                'ticker_name': d.ticker_name,
                'action': d.action,
                'executed': d.executed,
                'confidence': d.confidence,
                'reasoning': d.reasoning,
                'signal_values': d.signal_values,
            }
            for d in decisions[-5:]
        ],
        'error': error_message or None,
        'message': 'Dry-run completed' if error_message == '' else 'Dry-run failed',
    }
    _emit(payload, as_json=as_json)
    if error_message:
        raise click.ClickException(f'Dry-run failed: {error_message}')


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
    default='coinjure.strategy.test_strategy:TestStrategy',
    show_default=True,
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
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
    strategy_kwargs_json: str | None,
    as_json: bool,
) -> None:
    """Run backtest mode with historical data + paper execution."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
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
            'strategy_kwargs': strategy_kwargs,
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
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode (no orders).',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
def paper_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
) -> None:
    """Run paper trading in simulation mode."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else _IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    capital = Decimal(initial_capital)
    _emit(
        {
            'mode': 'paper',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'message': (
                f'Starting paper mode ({exchange})'
                if strategy_ref
                else f'Starting paper mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    if exchange == 'polymarket':
        data_source = _build_news_augmented_source('polymarket')
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Polymarket',
            )
        )
    elif exchange == 'kalshi':
        data_source = _build_news_augmented_source('kalshi')
        asyncio.run(
            run_live_kalshi_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name='Kalshi',
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
                monitor=monitor,
                exchange_name='RSS',
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
    default=None,
    help='Strategy ref: module:Class or /path/file.py:Class. If omitted, run in idle mode (no orders).',
)
@click.option(
    '--strategy-kwargs-json',
    default=None,
    help='JSON object for strategy constructor kwargs.',
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
@click.option(
    '--monitor', '-m', is_flag=True, default=False, help='Show live TUI dashboard'
)
def live_run(
    exchange: str,
    duration: float | None,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    wallet_private_key: str | None,
    signature_type: int,
    funder: str | None,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    monitor: bool,
) -> None:
    """Run live mode with real order placement."""
    _confirm_live_trading(as_json=as_json)

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    if strategy_kwargs and not strategy_ref:
        raise click.ClickException(
            '--strategy-kwargs-json requires --strategy-ref (idle mode has no strategy).'
        )
    strategy_obj = (
        _load_strategy(strategy_ref, strategy_kwargs)
        if strategy_ref
        else _IdleStrategy()
    )
    strategy_mode = 'active' if strategy_ref else 'idle'
    _emit(
        {
            'mode': 'live',
            'exchange': exchange,
            'strategy_ref': strategy_ref,
            'strategy_kwargs': strategy_kwargs,
            'strategy_mode': strategy_mode,
            'message': (
                f'Starting live mode ({exchange})'
                if strategy_ref
                else f'Starting live mode ({exchange}) in idle mode (no strategy orders)'
            ),
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
                monitor=monitor,
                exchange_name='Polymarket',
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
                continuous=True,
                monitor=monitor,
                exchange_name='Kalshi',
            )
        )

    _emit({'mode': 'live', 'message': 'Live session ended'}, as_json=as_json)
