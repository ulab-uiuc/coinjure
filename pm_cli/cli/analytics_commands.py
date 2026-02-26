"""CLI commands for reviewing historical trading performance from state files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import click

from pred_market_cli.analytics.performance_analyzer import PerformanceAnalyzer
from pred_market_cli.storage.state_store import StateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_store(state_dir: str) -> StateStore:
    path = Path(state_dir).expanduser().resolve()
    if not path.exists():
        raise click.ClickException(f'State directory not found: {path}')
    return StateStore(path)


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, default=str))
    else:
        if isinstance(payload, dict):
            click.echo(payload.get('message', str(payload)))
        else:
            click.echo(str(payload))


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def analytics() -> None:
    """Review historical trading performance from persisted state files."""


@analytics.command('summary')
@click.option(
    '--state-dir',
    default='.',
    show_default=True,
    type=click.Path(file_okay=False),
    help='Directory containing trades.json, positions.json, equity_curve.json.',
)
@click.option(
    '--initial-capital',
    default='10000',
    show_default=True,
    help='Initial capital used for return calculations.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def analytics_summary(state_dir: str, initial_capital: str, as_json: bool) -> None:
    """Print a performance summary (P&L, win rate, Sharpe, drawdown)."""
    store = _load_store(state_dir)
    trades = store.load_trades()

    capital = Decimal(initial_capital)
    analyzer = PerformanceAnalyzer(initial_capital=capital)
    for trade in trades:
        analyzer.add_trade(trade)

    stats = analyzer.get_stats()
    current_equity = analyzer.get_current_equity()
    return_pct = analyzer.get_return_pct()

    if as_json:
        payload = {
            'initial_capital': str(capital),
            'current_equity': str(current_equity),
            'return_pct': str(return_pct),
            'total_trades': stats.total_trades,
            'winning_trades': stats.winning_trades,
            'losing_trades': stats.losing_trades,
            'win_rate': str(stats.win_rate),
            'total_pnl': str(stats.total_pnl),
            'average_profit': str(stats.average_profit),
            'average_loss': str(stats.average_loss),
            'profit_factor': str(stats.profit_factor),
            'max_drawdown': str(stats.max_drawdown),
            'sharpe_ratio': str(stats.sharpe_ratio),
            'max_consecutive_wins': stats.max_consecutive_wins,
            'max_consecutive_losses': stats.max_consecutive_losses,
        }
        click.echo(json.dumps(payload))
        return

    # Human-readable output — reuse PerformanceAnalyzer.print_summary()
    click.echo(f'State directory: {Path(state_dir).resolve()}')
    click.echo(f'Trades loaded:   {len(trades)}')
    analyzer.print_summary()


@analytics.command('trades')
@click.option(
    '--state-dir', default='.', show_default=True, type=click.Path(file_okay=False)
)
@click.option(
    '--limit',
    default=50,
    show_default=True,
    type=int,
    help='Maximum number of trades to display.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def analytics_trades(state_dir: str, limit: int, as_json: bool) -> None:
    """Show trade history from a state directory."""
    store = _load_store(state_dir)
    trades = store.load_trades()

    if not trades:
        if as_json:
            click.echo(json.dumps({'count': 0, 'trades': []}))
        else:
            click.echo('No trades found.')
        return

    display = trades[-limit:] if len(trades) > limit else trades

    if as_json:
        from pred_market_cli.storage.serializers import serialize_trade

        payload = {
            'total': len(trades),
            'shown': len(display),
            'trades': [serialize_trade(t) for t in display],
        }
        click.echo(json.dumps(payload, default=str))
        return

    click.echo(
        f'Trade history ({len(display)} of {len(trades)} total, most recent last):\n'
    )
    click.echo(
        f'  {"#":<5} {"Side":<6} {"Symbol":<30} {"Price":<10} {"Qty":<10} {"Commission":<12}'
    )
    click.echo('  ' + '-' * 75)
    offset = len(trades) - len(display)
    for i, t in enumerate(display, start=offset + 1):
        symbol = t.ticker.symbol[:28]
        click.echo(
            f'  {i:<5} {t.side.value:<6} {symbol:<30} {t.price:<10.4f} {t.quantity:<10.4f} {t.commission:<12.6f}'
        )


@analytics.command('positions')
@click.option(
    '--state-dir', default='.', show_default=True, type=click.Path(file_okay=False)
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def analytics_positions(state_dir: str, as_json: bool) -> None:
    """Show open positions from a state directory."""
    store = _load_store(state_dir)
    positions = store.load_positions()

    if not positions:
        if as_json:
            click.echo(json.dumps({'count': 0, 'positions': []}))
        else:
            click.echo('No positions found.')
        return

    if as_json:
        from pred_market_cli.storage.serializers import serialize_position

        payload = {
            'count': len(positions),
            'positions': [serialize_position(p) for p in positions],
        }
        click.echo(json.dumps(payload, default=str))
        return

    click.echo(f'Open positions ({len(positions)} total):\n')
    click.echo(f'  {"Symbol":<35} {"Qty":<12} {"Avg Cost":<12} {"Realized PnL":<14}')
    click.echo('  ' + '-' * 73)
    for pos in positions:
        symbol = pos.ticker.symbol[:33]
        click.echo(
            f'  {symbol:<35} {pos.quantity:<12.4f} {pos.average_cost:<12.4f} {pos.realized_pnl:<14.4f}'
        )
