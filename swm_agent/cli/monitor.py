"""Trading monitor CLI command."""

from __future__ import annotations

import asyncio
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

from swm_agent.data.live.google_news_data_source import GoogleNewsDataSource
from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.events.events import NewsEvent
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import NoRiskManager
from swm_agent.ticker.ticker import CashTicker
from swm_agent.trader.paper_trader import PaperTrader
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import OrderStatus


class TradingMonitor:
    """Monitor for displaying trading engine state."""

    def __init__(
        self,
        trader: Trader,
        position_manager: PositionManager,
        exchange_name: str = '',
    ) -> None:
        self.trader = trader
        self.position_manager = position_manager
        self.exchange_name = exchange_name
        self.console = Console()
        self.start_time = datetime.now()

        # Attributes synced from engine by MonitoredTradingEngine._sync_data()
        self.llm_decisions: list = []
        self.total_executed: int = 0
        self.total_decisions: int = 0
        self.total_buy_yes: int = 0
        self.total_buy_no: int = 0
        self.total_holds: int = 0
        self.total_closes: int = 0
        self.activity_log: list[tuple[str, str]] = []
        self.news_headlines: list[tuple[str, str]] = []
        self.event_count: int = 0
        self.news_buffer_count: int = 0
        self.perf_stats = None
        self.ob_count: int = 0

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

        try:
            # Snapshot positions dict to avoid RuntimeError during iteration
            md = self.trader.market_data
            portfolio_value = self.position_manager.get_portfolio_value(md)
            total_value = sum(portfolio_value.values(), Decimal('0'))
            table.add_row(
                'Total Portfolio Value', f'${self._format_decimal(total_value, 2)}'
            )

            cash_positions = self.position_manager.get_cash_positions()
            for cash_pos in cash_positions:
                table.add_row(
                    f'  {cash_pos.ticker.symbol}',
                    f'${self._format_decimal(cash_pos.quantity, 2)}',
                )

            realized_pnl = self.position_manager.get_total_realized_pnl()
            unrealized_pnl = self.position_manager.get_total_unrealized_pnl(md)
            total_pnl = realized_pnl + unrealized_pnl

            table.add_row('Realized P&L', self._format_pnl(realized_pnl))
            table.add_row('Unrealized P&L', self._format_pnl(unrealized_pnl))
            table.add_row('Total P&L', self._format_pnl(total_pnl))

            # Exposure metrics
            non_cash = self.position_manager.get_non_cash_positions()
            market_exposure = Decimal('0')
            for p in non_cash:
                if p.quantity > 0:
                    bid = md.get_best_bid(p.ticker)
                    if bid:
                        market_exposure += bid.price * p.quantity

            if total_value > 0:
                exposure_pct = float(market_exposure / total_value * 100)
                table.add_row('Exposure', f'${self._format_decimal(market_exposure, 2)} ({exposure_pct:.1f}%)')
            else:
                table.add_row('Exposure', '$0.00 (0.0%)')
        except (RuntimeError, KeyError, AttributeError):
            table.add_row('', Text('updating...', style='dim'))

        return Panel(table, title='[bold]Portfolio Summary[/bold]', border_style='blue')


    def _create_llm_decisions_panel(self, limit: int = 12) -> Panel:
        """Create LLM decisions panel showing AI probability estimates vs market."""
        decisions: list = getattr(self, 'llm_decisions', [])[-limit:]

        if not decisions:
            return Panel(
                '[dim]Waiting for LLM analysis...[/dim]',
                title='[bold]LLM Decisions[/bold]',
                border_style='bright_magenta',
            )

        table = Table(show_header=True, header_style='bold', expand=True)
        table.add_column('Time', style='dim', width=8)
        table.add_column('Action', width=8)
        table.add_column('LLM', justify='right', width=5)
        table.add_column('Mkt', justify='right', width=5)
        table.add_column('Edge', justify='right', width=6)
        table.add_column('Market', width=22)
        table.add_column('Reasoning', ratio=1)
        table.add_column('', width=3)

        for d in reversed(decisions):
            action_style = {
                'BUY_YES': 'bold green',
                'BUY_NO': 'bold red',
                'HOLD': 'dim',
                'CLOSE_EDGE_TP': 'bold yellow',
                'CLOSE_EDGE_REV': 'bold red',
                'CLOSE_REEVAL': 'bold magenta',
                'CLOSE_TIMEOUT': 'yellow',
            }.get(d.action, 'white')

            llm_prob = getattr(d, 'llm_prob', 0.0) or 0.0
            mkt_price = getattr(d, 'market_price', 0.0) or 0.0
            edge = llm_prob - mkt_price

            edge_style = 'green' if edge > 0 else 'red' if edge < 0 else 'dim'
            edge_str = f'{edge:+.0%}' if mkt_price > 0 else '—'

            exec_text = Text('✓', style='bold green') if d.executed else Text('—', style='dim')
            reasoning = getattr(d, 'reasoning', '') or ''

            table.add_row(
                d.timestamp,
                Text(d.action, style=action_style),
                Text(f'{llm_prob:.0%}', style='white') if llm_prob > 0 else Text('—', style='dim'),
                Text(f'{mkt_price:.0%}', style='white') if mkt_price > 0 else Text('—', style='dim'),
                Text(edge_str, style=edge_style),
                d.ticker_name[:22],
                Text(reasoning[:45], style='dim'),
                exec_text,
            )

        return Panel(
            table, title='[bold]LLM Decisions (Prob vs Market)[/bold]', border_style='bright_magenta'
        )

    def _create_activity_log_panel(self, limit: int = 12) -> Panel:
        """Create scrolling activity log panel."""
        log_entries: list[tuple[str, str]] = getattr(self, 'activity_log', [])[-limit:]

        if not log_entries:
            return Panel(
                '[dim]Waiting for activity...[/dim]',
                title='[bold]Activity Log[/bold]',
                border_style='bright_cyan',
            )

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column('Time', style='dim', width=8)
        table.add_column('Event')

        for ts, msg in reversed(log_entries):
            # Color code based on content
            if 'BUY' in msg:
                style = 'green'
            elif 'SELL' in msg:
                style = 'red'
            elif 'Error' in msg:
                style = 'bold red'
            elif 'LLM' in msg:
                style = 'bright_magenta'
            elif 'News' in msg:
                style = 'cyan'
            else:
                style = 'white'

            table.add_row(ts, Text(msg[:80], style=style))

        return Panel(
            table, title='[bold]Activity Log[/bold]', border_style='bright_cyan'
        )

    def _create_stats_panel(self) -> Panel:
        """Create statistics panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column('Metric', style='cyan')
        table.add_column('Value', justify='right')

        # Runtime
        runtime = datetime.now() - self.start_time
        runtime_str = str(runtime).split('.')[0]
        table.add_row('Runtime', runtime_str)

        # Order statistics — snapshot the list to avoid RuntimeError
        try:
            orders = list(getattr(self.trader, 'orders', []))
        except RuntimeError:
            orders = []
        total_orders = len(orders)
        filled_orders = sum(1 for o in orders if o.status == OrderStatus.FILLED)
        rejected_orders = sum(1 for o in orders if o.status == OrderStatus.REJECTED)

        table.add_row('Total Orders', str(total_orders))
        table.add_row('Filled Orders', str(filled_orders))
        table.add_row('Rejected Orders', str(rejected_orders))

        if total_orders > 0:
            fill_rate = (filled_orders / total_orders) * 100
            table.add_row('Fill Rate', f'{fill_rate:.1f}%')

        # Position count (only positions with qty > 0)
        try:
            active_positions = sum(
                1 for p in self.position_manager.get_non_cash_positions()
                if p.quantity > 0
            )
        except RuntimeError:
            active_positions = 0
        table.add_row('Active Positions', str(active_positions))

        # LLM decision counts — use running counters (not deque which evicts old entries)
        total_decisions = getattr(self, 'total_decisions', 0)
        total_executed = getattr(self, 'total_executed', 0)
        buy_yes = getattr(self, 'total_buy_yes', 0)
        buy_no = getattr(self, 'total_buy_no', 0)
        holds = getattr(self, 'total_holds', 0)
        closes = getattr(self, 'total_closes', 0)
        table.add_row('LLM Decisions', str(total_decisions))
        table.add_row('  YES / NO / HOLD', f'{buy_yes} / {buy_no} / {holds}')
        table.add_row('  Closes', str(closes))
        table.add_row('  Executed', str(total_executed))

        # News buffer stats
        news_count = getattr(self, 'news_buffer_count', 0)
        table.add_row('  News Buffered', str(news_count))

        # Performance analyzer stats
        perf_stats = getattr(self, 'perf_stats', None)
        if perf_stats is not None and perf_stats.total_trades > 0:
            table.add_row('', '')  # separator
            table.add_row('[bold]Performance[/bold]', '')
            win_pct = f'{perf_stats.win_rate * 100:.1f}%'
            table.add_row('  Win Rate', Text(win_pct, style='green' if perf_stats.win_rate > Decimal('0.5') else 'red'))
            table.add_row('  Profit Factor', f'{perf_stats.profit_factor:.2f}')
            table.add_row('  Sharpe Ratio', f'{perf_stats.sharpe_ratio:.2f}')
            dd_pct = f'{perf_stats.max_drawdown * 100:.1f}%'
            table.add_row('  Max Drawdown', Text(dd_pct, style='red' if perf_stats.max_drawdown > Decimal('0.05') else 'white'))
            table.add_row('  Total PnL', self._format_pnl(perf_stats.total_pnl))
            table.add_row('  W/L Streak', f'{perf_stats.max_consecutive_wins}W / {perf_stats.max_consecutive_losses}L')

        return Panel(table, title='[bold]Statistics[/bold]', border_style='cyan')

    def _create_trading_panel(self, limit: int = 10) -> Panel:
        """Create combined positions + recent orders panel."""
        # Snapshot shared collections to avoid RuntimeError
        try:
            positions = list(self.position_manager.get_non_cash_positions())
        except RuntimeError:
            positions = []
        try:
            orders = list(getattr(self.trader, 'orders', []))[-limit:]
        except RuntimeError:
            orders = []

        text_parts: list[Text] = []

        # Positions section
        if positions:
            text_parts.append(Text('── Positions ──\n', style='bold green'))
            for pos in positions:
                if pos.quantity <= 0:
                    continue
                # Try bid first, fallback to ask
                md = self.trader.market_data
                best_bid = md.get_best_bid(pos.ticker)
                cur = best_bid.price if best_bid else Decimal('0')
                if cur <= 0:
                    best_ask = md.get_best_ask(pos.ticker)
                    cur = best_ask.price if best_ask else Decimal('0')
                pnl = (cur - pos.average_cost) * pos.quantity if cur > 0 else Decimal('0')
                pnl_style = 'green' if pnl >= 0 else 'red'
                line = Text()
                display_name = getattr(pos.ticker, 'name', '') or pos.ticker.symbol
                line.append(f'  {display_name[:28]:<28}', style='cyan')
                line.append(f' qty={pos.quantity:<6}', style='white')
                line.append(f' cost=${pos.average_cost:.4f}', style='dim')
                if cur > 0:
                    line.append(f' now=${cur:.4f}', style='white')
                    line.append(f' pnl=${pnl:+.2f}', style=pnl_style)
                else:
                    line.append(' now=N/A', style='dim')
                    line.append(' pnl=N/A', style='dim')
                line.append('\n')
                text_parts.append(line)
        else:
            text_parts.append(Text('── Positions ──\n', style='bold green'))
            text_parts.append(Text('  No positions yet\n', style='dim'))

        text_parts.append(Text('\n'))

        # Orders section
        if orders:
            text_parts.append(Text('── Recent Orders ──\n', style='bold yellow'))
            for order in reversed(orders[-8:]):
                side_style = 'green' if order.side.value == 'buy' else 'red'
                status_style = 'green' if order.status == OrderStatus.FILLED else 'yellow'
                line = Text()
                line.append(f'  {order.side.value.upper():<5}', style=side_style)
                order_name = getattr(order.ticker, 'name', '') or order.ticker.symbol
                line.append(f' {order_name[:25]:<25}', style='cyan')
                line.append(f' ${order.limit_price:.4f}', style='white')
                line.append(f' filled={order.filled_quantity}', style='dim')
                line.append(f' [{order.status.value.upper()}]', style=status_style)
                line.append('\n')
                text_parts.append(line)
        else:
            text_parts.append(Text('── Recent Orders ──\n', style='bold yellow'))
            text_parts.append(Text('  No orders yet\n', style='dim'))

        combined = Text()
        for part in text_parts:
            combined.append_text(part)

        return Panel(combined, title='[bold]Trading Activity[/bold]', border_style='yellow')

    def _create_orderbook_panel(self, limit: int = 8) -> Panel:
        """Create order book panel showing top markets with bid/ask/spread."""
        try:
            # Snapshot to avoid RuntimeError
            ob_items = list(self.trader.market_data.order_books.items())
        except (RuntimeError, AttributeError):
            ob_items = []

        # Filter: skip cash tickers, empty books, and extreme probability markets
        active_books: list[tuple] = []
        for ticker, ob in ob_items:
            if isinstance(ticker, CashTicker):
                continue
            best_bid = ob.best_bid
            best_ask = ob.best_ask
            if not (best_bid and best_ask and best_bid.price > 0):
                continue
            mid = (best_bid.price + best_ask.price) / 2
            # Skip extreme markets (< 5% or > 95%) — uninteresting
            if mid < Decimal('0.05') or mid > Decimal('0.95'):
                continue
            spread = best_ask.price - best_bid.price
            active_books.append((ticker, best_bid, best_ask, spread, mid))

        if not active_books:
            return Panel(
                '[dim]Waiting for order book data...[/dim]',
                title='[bold]Order Books[/bold]',
                border_style='green',
            )

        # Sort by closeness to 50% (most interesting markets first)
        active_books.sort(key=lambda x: abs(x[4] - Decimal('0.5')))

        table = Table(show_header=True, header_style='bold', expand=True)
        table.add_column('Market', ratio=2)
        table.add_column('Bid', justify='right', width=8)
        table.add_column('Ask', justify='right', width=8)
        table.add_column('Sprd', justify='right', width=7)
        table.add_column('Mid', justify='right', width=6)

        for ticker, bid, ask, spread, mid in active_books[:limit]:
            name = getattr(ticker, 'name', '') or ticker.symbol
            spread_style = 'green' if spread <= Decimal('0.02') else 'yellow' if spread <= Decimal('0.05') else 'red'
            mid_pct = f'{mid * 100:.0f}%'
            table.add_row(
                name[:30],
                f'${bid.price:.4f}',
                f'${ask.price:.4f}',
                Text(f'{spread:.4f}', style=spread_style),
                mid_pct,
            )

        total_ob = getattr(self, 'ob_count', len(ob_items))
        return Panel(
            table,
            title=f'[bold]Order Books ({len(active_books)}/{total_ob} active)[/bold]',
            border_style='green',
        )

    def _create_news_panel(self, limit: int = 10) -> Panel:
        """Create news headlines panel."""
        news: list[tuple[str, str]] = getattr(self, 'news_headlines', [])

        if not news:
            return Panel(
                '[dim]Waiting for news...[/dim]',
                title='[bold]News Headlines[/bold]',
                border_style='bright_yellow',
            )

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column('Time', style='dim', width=8)
        table.add_column('Headline')

        # Show most recent first
        for ts, headline in reversed(news[-limit:]):
            table.add_row(ts, Text(headline[:70], style='white'))

        return Panel(
            table,
            title=f'[bold]News Headlines ({len(news)} total)[/bold]',
            border_style='bright_yellow',
        )

    def _create_system_status_panel(self) -> Panel:
        """Create compact system status bar."""
        event_count = getattr(self, 'event_count', 0)
        ob_count = getattr(self, 'ob_count', 0)
        news_count = len(getattr(self, 'news_headlines', []))
        news_buf = getattr(self, 'news_buffer_count', 0)

        # Runtime
        runtime = datetime.now() - self.start_time
        runtime_str = str(runtime).split('.')[0]

        status_parts = [
            f'Runtime: {runtime_str}',
            f'Events: {event_count}',
            f'Order Books: {ob_count}',
            f'News: {news_count}',
            f'News Buffer: {news_buf}',
        ]
        status_line = Text('  |  '.join(status_parts), style='white')

        return Panel(
            status_line,
            title='[bold]System Status[/bold]',
            border_style='bright_green',
        )

    def create_layout(self) -> Layout:
        """Create the full monitoring layout."""
        layout = Layout()

        # Header + body + status bar + footer
        layout.split_column(
            Layout(name='header', size=3),
            Layout(name='body'),
            Layout(name='status_bar', size=3),
            Layout(name='footer', size=1),
        )

        # Header with exchange name
        title = 'SWM Agent - Trading Monitor'
        if self.exchange_name:
            title = f'SWM Agent - {self.exchange_name} Trading Monitor'
        header_text = Text(title, justify='center', style='bold blue')
        layout['header'].update(Panel(header_text, border_style='blue'))

        # Body: 3 rows
        layout['body'].split_column(
            Layout(name='top_row', ratio=3),
            Layout(name='mid_row', ratio=3),
            Layout(name='bot_row', ratio=3),
        )

        # Top row: Portfolio + Stats | LLM Decisions (main focus)
        layout['top_row'].split_row(
            Layout(name='left_top', ratio=1),
            Layout(name='llm_decisions', ratio=2),
        )

        layout['left_top'].split_column(
            Layout(name='portfolio'),
            Layout(name='stats'),
        )

        # Mid row: Trading Activity (positions + orders) | Activity Log
        layout['mid_row'].split_row(
            Layout(name='trading', ratio=1),
            Layout(name='activity_log', ratio=1),
        )

        # Bottom row: Order Books | News Headlines
        layout['bot_row'].split_row(
            Layout(name='orderbooks', ratio=1),
            Layout(name='news', ratio=1),
        )

        # Populate panels
        layout['portfolio'].update(self._create_portfolio_summary())
        layout['stats'].update(self._create_stats_panel())
        layout['llm_decisions'].update(self._create_llm_decisions_panel())
        layout['trading'].update(self._create_trading_panel())
        layout['activity_log'].update(self._create_activity_log_panel())
        layout['orderbooks'].update(self._create_orderbook_panel())
        layout['news'].update(self._create_news_panel())

        # System status bar
        layout['status_bar'].update(self._create_system_status_panel())

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


def _drain_news_queue(
    news_ds: GoogleNewsDataSource,
    headlines: list[NewsEvent],
) -> None:
    """Move pending events from the data source queue into *headlines*."""
    while not news_ds.event_queue.empty():
        try:
            event = news_ds.event_queue.get_nowait()
            if isinstance(event, NewsEvent):
                headlines.append(event)
        except asyncio.QueueEmpty:
            break


async def _run_monitor(watch: bool, refresh: float) -> None:
    """Async entry point that wires components and runs the monitor."""
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('1'),
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

    headlines: list[NewsEvent] = []
    news_ds = GoogleNewsDataSource(polling_interval=60.0)

    mon = TradingMonitor(trader, position_manager)
    mon.news_headlines = headlines  # type: ignore[attr-defined]

    await news_ds.start()
    try:
        if watch:
            with Live(
                mon.create_layout(),
                console=mon.console,
                refresh_per_second=4,
                screen=True,
            ) as live:
                while True:
                    _drain_news_queue(news_ds, headlines)
                    live.update(mon.create_layout())
                    await asyncio.sleep(refresh)
        else:
            _drain_news_queue(news_ds, headlines)
            mon.display_snapshot()
    except asyncio.CancelledError:
        pass
    finally:
        await news_ds.stop()


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
def monitor(watch: bool, refresh: float) -> None:
    """Monitor trading activities, positions, and portfolio status.

    Display current portfolio value, active positions with P&L,
    recent orders, market data, and trading statistics.

    Examples:
        swm-agent monitor               # Single snapshot
        swm-agent monitor --watch       # Live updating mode
        swm-agent monitor -w -r 1.0     # Live mode with 1 second refresh
    """
    try:
        asyncio.run(_run_monitor(watch, refresh))
    except KeyboardInterrupt:
        pass
