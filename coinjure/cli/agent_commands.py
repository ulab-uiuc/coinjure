"""Agent-first CLI commands for strategy, backtest, paper, and live modes."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from coinjure.backtest.backtester import run_backtest, run_backtest_parquet
from coinjure.cli.utils import _emit
from coinjure.data.composite_data_source import CompositeDataSource
from coinjure.data.live.kalshi_data_source import LiveKalshiDataSource
from coinjure.data.live.live_data_source import LivePolyMarketDataSource
from coinjure.events.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.live.live_trader import (
    run_live_kalshi_paper_trading,
    run_live_kalshi_trading,
    run_live_paper_trading,
    run_live_polymarket_trading,
)
from coinjure.strategy.loader import load_strategy_class as _shared_load_strategy_class
from coinjure.strategy.strategy import Strategy
from coinjure.ticker.ticker import PolyMarketTicker
from coinjure.trader.trader import Trader


class _IdleStrategy(Strategy):
    """No-op strategy: consume events without placing orders."""

    async def process_event(self, event: Event, trader: Trader) -> None:
        return


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


def _build_market_source(
    exchange: str,
) -> CompositeDataSource | LivePolyMarketDataSource | LiveKalshiDataSource:
    """Build a market data source for the given exchange.

    - polymarket      → LivePolyMarketDataSource
    - kalshi          → LiveKalshiDataSource
    - cross_platform  → CompositeDataSource([poly, kalshi]) — required for cross-platform arb
    """
    if exchange == 'polymarket':
        return LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
    if exchange == 'kalshi':
        return LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
    if exchange == 'cross_platform':
        poly = LivePolyMarketDataSource(
            event_cache_file='events_cache.jsonl',
            polling_interval=60.0,
            orderbook_refresh_interval=10.0,
            reprocess_on_start=False,
        )
        kalshi = LiveKalshiDataSource(
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        return CompositeDataSource([poly, kalshi])
    raise click.ClickException(f'Unsupported exchange: {exchange!r}')


def _load_strategy_class(strategy_ref: str) -> type[Strategy]:
    try:
        return _shared_load_strategy_class(strategy_ref)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


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
                    timestamp=None,
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
    '--dry-run',
    'do_dry_run',
    is_flag=True,
    default=False,
    help='Also feed mock events to confirm runtime behaviour.',
)
@click.option(
    '--events',
    default=8,
    show_default=True,
    type=click.IntRange(1, 50),
    help='Mock events to feed when --dry-run is set.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON result')
def strategy_validate(
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    do_dry_run: bool,
    events: int,
    as_json: bool,
) -> None:
    """Validate that a strategy is importable, constructible, and (optionally) runtime-safe."""
    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)

    payload: dict[str, Any] = {
        'ok': True,
        'strategy_ref': strategy_ref,
        'strategy_kwargs': strategy_kwargs,
        'class': strategy_obj.__class__.__name__,
        'module': strategy_obj.__class__.__module__,
        'message': f'Valid strategy: {strategy_ref}',
    }

    if do_dry_run:
        from coinjure.data.market_data_manager import MarketDataManager
        from coinjure.position.position_manager import Position, PositionManager
        from coinjure.risk.risk_manager import NoRiskManager
        from coinjure.ticker.ticker import CashTicker
        from coinjure.trader.paper_trader import PaperTrader

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
        payload.update(
            {
                'ok': error_message == '',
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
                'message': 'Dry-run completed'
                if error_message == ''
                else 'Dry-run failed',
            }
        )
        _emit(payload, as_json=as_json)
        if error_message:
            raise click.ClickException(f'Dry-run failed: {error_message}')
        return

    _emit(payload, as_json=as_json)


@click.group()
def backtest() -> None:
    """Backtest commands."""


@backtest.command('run')
@click.option(
    '--history-file',
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help='JSONL history file (mutually exclusive with --parquet).',
)
@click.option(
    '--parquet',
    'parquet_path',
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help='Parquet orderbook snapshot file from pmxt archive.',
)
@click.option('--symbol', default='BACKTEST_TOKEN', show_default=True)
@click.option('--name', default='Backtest Market', show_default=True)
@click.option('--market-id', default=None)
@click.option('--event-id', default=None)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--spread',
    default='0.01',
    show_default=True,
    help='Synthetic bid-ask spread for simulated order book.',
)
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
@click.option('--min-fill-rate', default='0.5', show_default=True)
@click.option('--max-fill-rate', default='1.0', show_default=True)
@click.option('--commission-rate', default='0.0', show_default=True)
@click.option(
    '--risk-profile',
    default='none',
    show_default=True,
    type=click.Choice(['none', 'standard']),
)
@click.option(
    '--all-markets-context/--primary-market-context',
    default=False,
    show_default=True,
    help='Expose all markets from the history file to the strategy context.',
)
@click.option(
    '--allow-cross-market-trading/--primary-market-only',
    default=False,
    show_default=True,
    help='Allow the strategy to place trades outside the requested market.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON status')
def backtest_run(
    history_file: str | None,
    parquet_path: str | None,
    symbol: str,
    name: str,
    market_id: str | None,
    event_id: str | None,
    initial_capital: str,
    spread: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    min_fill_rate: str,
    max_fill_rate: str,
    commission_rate: str,
    risk_profile: str,
    all_markets_context: bool,
    allow_cross_market_trading: bool,
    as_json: bool,
) -> None:
    """Run backtest mode with historical data + paper execution."""
    if not history_file and not parquet_path:
        raise click.ClickException('Provide either --history-file or --parquet.')
    if history_file and parquet_path:
        raise click.ClickException(
            '--history-file and --parquet are mutually exclusive.'
        )

    # Parquet mode — simpler path, real orderbook data
    if parquet_path:
        strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
        strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
        capital = Decimal(initial_capital)
        _emit(
            {
                'mode': 'backtest_parquet',
                'message': f'Starting parquet backtest: {strategy_ref}',
                'parquet_path': parquet_path,
                'market_id': market_id,
                'strategy_kwargs': strategy_kwargs,
            },
            as_json=as_json,
        )
        asyncio.run(
            run_backtest_parquet(
                parquet_path=parquet_path,
                initial_capital=capital,
                strategy=strategy_obj,
                market_id=market_id,
            )
        )
        _emit(
            {'mode': 'backtest_parquet', 'message': 'Parquet backtest completed'},
            as_json=as_json,
        )
        return

    if not market_id:
        raise click.ClickException('--market-id is required for history-file backtest.')
    if not event_id:
        raise click.ClickException('--event-id is required for history-file backtest.')

    strategy_kwargs = _parse_strategy_kwargs_json(strategy_kwargs_json)
    capital = Decimal(initial_capital)
    try:
        fill_min = Decimal(min_fill_rate)
        fill_max = Decimal(max_fill_rate)
        fee = Decimal(commission_rate)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            'Invalid fill/commission value. Use numeric decimals.'
        ) from exc
    if fill_min <= 0 or fill_max <= 0:
        raise click.ClickException('--min-fill-rate and --max-fill-rate must be > 0.')
    if fill_min > fill_max:
        raise click.ClickException('--min-fill-rate cannot exceed --max-fill-rate.')
    if fee < 0:
        raise click.ClickException('--commission-rate must be >= 0.')

    if as_json:
        from coinjure.cli.research_commands import _run_backtest_once

        try:
            metrics = _run_backtest_once(
                history_file=history_file,
                strategy_ref=strategy_ref,
                strategy_kwargs=strategy_kwargs,
                market_id=market_id,
                event_id=event_id,
                initial_capital=capital,
                min_fill_rate=fill_min,
                max_fill_rate=fill_max,
                commission_rate=fee,
                risk_profile=risk_profile,
                include_all_markets_context=all_markets_context,
                allow_cross_market_trading=allow_cross_market_trading,
            )
        except Exception as exc:  # noqa: BLE001
            _emit({'ok': False, 'error': str(exc)}, as_json=True)
            raise click.ClickException(str(exc)) from exc
        _emit({'ok': True, **metrics}, as_json=True)
        return

    if (
        fill_min != Decimal('0.5')
        or fill_max != Decimal('1.0')
        or fee != Decimal('0.0')
        or risk_profile != 'none'
    ):
        raise click.ClickException(
            'Custom fill/fee/risk options currently require --json mode.'
        )

    spread_val = Decimal(spread)
    strategy_obj = _load_strategy(strategy_ref, strategy_kwargs)
    no_symbol = f'{symbol}_NO'
    ticker = PolyMarketTicker(
        symbol=symbol,
        name=name,
        market_id=market_id,
        event_id=event_id,
        token_id=symbol,
        no_token_id=no_symbol,
    )
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
            spread=spread_val,
            include_all_markets_context=all_markets_context,
            allow_cross_market_trading=allow_cross_market_trading,
        )
    )
    _emit({'mode': 'backtest', 'message': 'Backtest completed'}, as_json=as_json)


@click.group()
def paper() -> None:
    """Paper trading commands (live data + simulated execution)."""


@paper.command('run')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'cross_platform']),
    default='polymarket',
    show_default=True,
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
@click.option(
    '--socket-path',
    default=None,
    type=click.Path(),
    help='Unix socket path for the control server (default: ~/.coinjure/engine.sock).',
)
@click.option(
    '--hub-socket',
    default=None,
    type=click.Path(),
    help='Connect to a running Market Data Hub instead of polling exchanges directly.',
)
def paper_run(
    exchange: str,
    duration: float | None,
    initial_capital: str,
    strategy_ref: str,
    strategy_kwargs_json: str | None,
    as_json: bool,
    monitor: bool,
    socket_path: str | None,
    hub_socket: str | None,
) -> None:
    """Run paper trading against a live exchange feed."""
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
            'hub_socket': hub_socket,
            'message': (
                f'Starting paper mode ({exchange})'
                if strategy_ref
                else f'Starting paper mode ({exchange}) in idle mode (no strategy orders)'
            ),
        },
        as_json=as_json,
    )

    _socket_path = Path(socket_path) if socket_path else None
    if hub_socket:
        from coinjure.data.hub.subscriber import HubDataSource

        data_source = HubDataSource(Path(hub_socket).expanduser())
    else:
        data_source = _build_market_source(exchange)
    exchange_label = {
        'polymarket': 'Polymarket',
        'kalshi': 'Kalshi',
        'cross_platform': 'Cross-Platform',
    }.get(exchange, exchange)

    if exchange == 'kalshi':
        asyncio.run(
            run_live_kalshi_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name=exchange_label,
                emit_text=not as_json,
                socket_path=_socket_path,
            )
        )
    else:
        # polymarket and cross_platform both use PaperTrader + POLYMARKET_USDC
        asyncio.run(
            run_live_paper_trading(
                data_source=data_source,
                strategy=strategy_obj,
                initial_capital=capital,
                duration=duration,
                continuous=True,
                monitor=monitor,
                exchange_name=exchange_label,
                emit_text=not as_json,
                socket_path=_socket_path,
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
        kalshi_source = LiveKalshiDataSource(
            api_key_id=kalshi_api_key_id,
            private_key_path=kalshi_private_key_path,
            event_cache_file='kalshi_events_cache.jsonl',
            polling_interval=60.0,
            reprocess_on_start=False,
        )
        asyncio.run(
            run_live_kalshi_trading(
                data_source=kalshi_source,
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
