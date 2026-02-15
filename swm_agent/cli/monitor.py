"""Trading monitor CLI command."""

import time
from datetime import datetime
from decimal import Decimal

import click
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swm_agent.position.position_manager import PositionManager
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import OrderStatus


class TradingMonitor:
    """Monitor for displaying trading engine state."""

    def __init__(self, trader: Trader, position_manager: PositionManager) -> None:
        self.trader = trader
        self.position_manager = position_manager
        self.console = Console()
        self.start_time = datetime.now()

    def _format_decimal(self, value: Decimal, decimals: int = 4) -> str:
        """Format decimal with specified precision."""
        return f'{value:.{decimals}f}'

    def _format_pnl(self, value: Decimal) -> Text:
        """Format P&L with color coding."""
        formatted = self._format_decimal(value, 2)
        if value > 0:
            return Text(f'+${formatted}', style='green')
        elif value < 0:
            return Text(f'-${abs(value):.2f}', style='red')
        return Text(f'${formatted}', style='white')

    def _create_portfolio_summary(self) -> Panel:
        """Create portfolio summary panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', justify='right')

        # Portfolio value
        portfolio_value = self.position_manager.get_portfolio_value(
            self.trader.market_data
        )
        total_value = sum(portfolio_value.values(), Decimal('0'))
        table.add_row(
            'Total Portfolio Value', f'${self._format_decimal(total_value, 2)}'
        )

        # Cash positions
        cash_positions = self.position_manager.get_cash_positions()
        for cash_pos in cash_positions:
            table.add_row(
                f'  {cash_pos.ticker.symbol}',
                f'${self._format_decimal(cash_pos.quantity, 2)}',
            )

        # P&L
        realized_pnl = self.position_manager.get_total_realized_pnl()
        unrealized_pnl = self.position_manager.get_total_unrealized_pnl(
            self.trader.market_data
        )
        total_pnl = realized_pnl + unrealized_pnl

        table.add_row('Realized P&L', self._format_pnl(realized_pnl))
        table.add_row('Unrealized P&L', self._format_pnl(unrealized_pnl))
        table.add_row('Total P&L', self._format_pnl(total_pnl))

        return Panel(table, title='[bold]Portfolio Summary[/bold]', border_style='blue')

    def _create_positions_table(self) -> Panel:
        """Create active positions table."""
        positions = self.position_manager.get_non_cash_positions()

        if not positions:
            return Panel(
                '[dim]No active positions[/dim]',
                title='[bold]Active Positions[/bold]',
                border_style='green',
            )

        table = Table(show_header=True, header_style='bold')
        table.add_column('Ticker', style='cyan')
        table.add_column('Quantity', justify='right')
        table.add_column('Avg Cost', justify='right')
        table.add_column('Current', justify='right')
        table.add_column('Unrealized P&L', justify='right')
        table.add_column('Realized P&L', justify='right')

        for pos in positions:
            best_bid = self.trader.market_data.get_best_bid(pos.ticker)
            current_price = best_bid.price if best_bid else Decimal('0')
            unrealized_pnl = self.position_manager.get_unrealized_pnl(
                pos.ticker, self.trader.market_data
            )

            table.add_row(
                pos.ticker.symbol,
                self._format_decimal(pos.quantity, 2),
                f'${self._format_decimal(pos.average_cost, 4)}',
                f'${self._format_decimal(current_price, 4)}',
                self._format_pnl(unrealized_pnl),
                self._format_pnl(pos.realized_pnl),
            )

        return Panel(table, title='[bold]Active Positions[/bold]', border_style='green')

    def _create_orders_table(self, limit: int = 10) -> Panel:
        """Create recent orders table."""
        orders = getattr(self.trader, 'orders', [])[-limit:]  # Get last N orders

        if not orders:
            return Panel(
                '[dim]No orders yet[/dim]',
                title='[bold]Recent Orders[/bold]',
                border_style='yellow',
            )

        table = Table(show_header=True, header_style='bold')
        table.add_column('Status', style='white')
        table.add_column('Side', style='white')
        table.add_column('Ticker', style='cyan')
        table.add_column('Limit Price', justify='right')
        table.add_column('Filled Qty', justify='right')
        table.add_column('Avg Price', justify='right')
        table.add_column('Commission', justify='right')

        for order in reversed(orders):  # Show most recent first
            # Status color coding
            status_style = {
                OrderStatus.FILLED: 'green',
                OrderStatus.PARTIALLY_FILLED: 'yellow',
                OrderStatus.REJECTED: 'red',
                OrderStatus.CANCELLED: 'dim',
            }.get(order.status, 'white')

            side_style = 'green' if order.side.value == 'buy' else 'red'

            table.add_row(
                Text(order.status.value.upper(), style=status_style),
                Text(order.side.value.upper(), style=side_style),
                order.ticker.symbol,
                f'${self._format_decimal(order.limit_price, 4)}',
                self._format_decimal(order.filled_quantity, 2),
                f'${self._format_decimal(order.average_price, 4)}'
                if order.average_price > 0
                else '-',
                f'${self._format_decimal(order.commission, 4)}',
            )

        return Panel(table, title='[bold]Recent Orders[/bold]', border_style='yellow')

    def _create_market_snapshot(self) -> Panel:
        """Create market data snapshot."""
        market_data = self.trader.market_data
        tickers = list(
            {pos.ticker for pos in self.position_manager.get_non_cash_positions()}
        )

        if not tickers:
            return Panel(
                '[dim]No market data available[/dim]',
                title='[bold]Market Snapshot[/bold]',
                border_style='magenta',
            )

        table = Table(show_header=True, header_style='bold')
        table.add_column('Ticker', style='cyan')
        table.add_column('Best Bid', justify='right')
        table.add_column('Best Ask', justify='right')
        table.add_column('Spread', justify='right')
        table.add_column('Spread %', justify='right')

        for ticker in tickers:
            best_bid = market_data.get_best_bid(ticker)
            best_ask = market_data.get_best_ask(ticker)
            bid = best_bid.price if best_bid else Decimal('0')
            ask = best_ask.price if best_ask else Decimal('0')
            spread = ask - bid
            spread_pct = (spread / bid * 100) if bid > 0 else Decimal('0')

            table.add_row(
                ticker.symbol,
                f'${self._format_decimal(bid, 4)}',
                f'${self._format_decimal(ask, 4)}',
                f'${self._format_decimal(spread, 4)}',
                f'{self._format_decimal(spread_pct, 2)}%',
            )

        return Panel(
            table, title='[bold]Market Snapshot[/bold]', border_style='magenta'
        )

    def _create_stats_panel(self) -> Panel:
        """Create statistics panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', justify='right')

        # Runtime
        runtime = datetime.now() - self.start_time
        runtime_str = str(runtime).split('.')[0]  # Remove microseconds
        table.add_row('Runtime', runtime_str)

        # Order statistics
        orders = getattr(self.trader, 'orders', [])
        total_orders = len(orders)
        filled_orders = sum(1 for o in orders if o.status == OrderStatus.FILLED)
        rejected_orders = sum(1 for o in orders if o.status == OrderStatus.REJECTED)

        table.add_row('Total Orders', str(total_orders))
        table.add_row('Filled Orders', str(filled_orders))
        table.add_row('Rejected Orders', str(rejected_orders))

        if total_orders > 0:
            success_rate = (filled_orders / total_orders) * 100
            table.add_row('Success Rate', f'{success_rate:.1f}%')

        # Position count
        active_positions = len(self.position_manager.get_non_cash_positions())
        table.add_row('Active Positions', str(active_positions))

        return Panel(table, title='[bold]Statistics[/bold]', border_style='cyan')

    def create_layout(self) -> Layout:
        """Create the full monitoring layout."""
        layout = Layout()

        # Main vertical split
        layout.split_column(
            Layout(name='header', size=3),
            Layout(name='body'),
            Layout(name='footer', size=1),
        )

        # Header
        header_text = Text(
            'SWM Agent - Trading Monitor', justify='center', style='bold blue'
        )
        layout['header'].update(Panel(header_text, border_style='blue'))

        # Body with 2x2 grid
        layout['body'].split_row(Layout(name='left'), Layout(name='right'))

        layout['left'].split_column(
            Layout(name='portfolio', ratio=1), Layout(name='positions', ratio=2)
        )

        layout['right'].split_column(
            Layout(name='orders', ratio=2), Layout(name='bottom_row', ratio=1)
        )

        layout['bottom_row'].split_row(
            Layout(name='market', ratio=1), Layout(name='stats', ratio=1)
        )

        # Populate panels
        layout['portfolio'].update(self._create_portfolio_summary())
        layout['positions'].update(self._create_positions_table())
        layout['orders'].update(self._create_orders_table())
        layout['market'].update(self._create_market_snapshot())
        layout['stats'].update(self._create_stats_panel())

        # Footer
        footer_text = Text(
            'Press Ctrl+C to exit | Last updated: '
            + datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            justify='center',
            style='dim',
        )
        layout['footer'].update(footer_text)

        return layout

    def display_snapshot(self) -> None:
        """Display a single snapshot of current state."""
        layout = self.create_layout()
        self.console.print(layout)

    def display_live(self, refresh_rate: float = 1.0) -> None:
        """Display live updating monitor."""
        with Live(
            self.create_layout(),
            console=self.console,
            refresh_per_second=1 / refresh_rate,
            screen=True,
        ) as live:
            try:
                while True:
                    time.sleep(refresh_rate)
                    live.update(self.create_layout())
            except KeyboardInterrupt:
                pass


@click.command()
@click.option(
    '--watch',
    '-w',
    is_flag=True,
    help='Enable live monitoring mode with continuous updates',
)
@click.option(
    '--refresh',
    '-r',
    default=2.0,
    type=float,
    help='Refresh rate in seconds for watch mode (default: 2.0)',
)
@click.option(
    '--config',
    '-c',
    type=click.Path(exists=True),
    help='Path to trading engine config file (if applicable)',
)
def monitor(watch: bool, refresh: float, config: str | None) -> None:
    """Monitor trading activities, positions, and portfolio status.

    Display current portfolio value, active positions with P&L,
    recent orders, market data, and trading statistics.

    Examples:
        swm-agent monitor               # Single snapshot
        swm-agent monitor --watch       # Live updating mode
        swm-agent monitor -w -r 1.0     # Live mode with 1 second refresh
    """
    # For demonstration purposes, this is a placeholder
    # In production, you would instantiate the actual trading engine
    click.echo(
        'Monitor command is ready! To use it, you need to integrate it with your '
        'trading engine.'
    )
    click.echo('\nTo integrate:')
    click.echo('1. Import your trading engine, trader, and position manager')
    click.echo('2. Pass them to TradingMonitor')
    click.echo('3. Call monitor.display_snapshot() or monitor.display_live()')
    click.echo('\nExample integration:')
    click.echo('    from swm_agent.core.trading_engine import TradingEngine')
    click.echo('    # ... initialize your trading engine ...')
    click.echo('    monitor = TradingMonitor(trader, position_manager)')
    click.echo('    if watch:')
    click.echo('        monitor.display_live(refresh_rate=refresh)')
    click.echo('    else:')
    click.echo('        monitor.display_snapshot()')
