"""Shared helper functions for strategy loading, market source building, and mock events.

This module contains no Click commands — all CLI groups/commands have been moved
to strategy_commands.py and engine_commands.py.  The helpers here are imported by
those modules and by research_helpers.py.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import click

from coinjure.data.data_source import CompositeDataSource
from coinjure.data.live.kalshi_data_source import LiveKalshiDataSource
from coinjure.data.live.polymarket_data_source import LivePolyMarketDataSource
from coinjure.engine.trader.trader import Trader
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.loader import load_strategy_class as _shared_load_strategy_class
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import PolyMarketTicker


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
