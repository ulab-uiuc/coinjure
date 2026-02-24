"""Textual-based interactive trading monitor for SWM Agent.

Key bindings (native, zero-latency):
  q / Ctrl+C  — quit
  p           — pause / resume data refresh
  Tab         — focus next panel
  Shift+Tab   — focus previous panel
  Arrow keys  — scroll / navigate within focused panel
  j / k       — scroll down / up within focused panel
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

if TYPE_CHECKING:
    from swm_agent.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────


def _fmt_pnl(v: Decimal) -> str:
    if v > 0:
        return f'[green]+${v:.2f}[/green]'
    if v < 0:
        return f'[red]-${abs(v):.2f}[/red]'
    return f'${v:.2f}'


# ── Panels ─────────────────────────────────────────────────────────────


class PortfolioPanel(Static):
    """Portfolio summary (Static text, updates in-place)."""

    DEFAULT_CSS = """
    PortfolioPanel {
        border: solid blue;
        padding: 0 1;
        height: 1fr;
    }
    """

    def refresh_data(self, trader, position_manager) -> None:
        try:
            md = trader.market_data
            pv = position_manager.get_portfolio_value(md)
            total = sum(pv.values(), Decimal('0'))
            realized = position_manager.get_total_realized_pnl()
            unrealized = position_manager.get_total_unrealized_pnl(md)

            lines = [
                '[bold blue]Portfolio Summary[/bold blue]',
                f'Total:          [bold]${total:>10.2f}[/bold]',
            ]
            for p in position_manager.get_cash_positions():
                lines.append(f'  {p.ticker.symbol:<18} ${p.quantity:>8.2f}')
            lines += [
                f'Realized P&L:   {_fmt_pnl(realized)}',
                f'Unrealized P&L: {_fmt_pnl(unrealized)}',
                f'Total P&L:      {_fmt_pnl(realized + unrealized)}',
            ]
            self.update('\n'.join(lines))
        except Exception:
            pass


class StatsPanel(Static):
    """Runtime statistics (Static text, updates in-place)."""

    DEFAULT_CSS = """
    StatsPanel {
        border: solid cyan;
        padding: 0 1;
        height: 1fr;
    }
    """

    def __init__(self, start_time: datetime, **kwargs) -> None:
        super().__init__(**kwargs)
        self.start_time = start_time

    def refresh_data(self, trader, position_manager, strategy, engine) -> None:
        try:
            runtime = str(datetime.now() - self.start_time).split('.')[0]
            orders = list(getattr(trader, 'orders', []))
            filled = sum(1 for o in orders if o.status.value == 'filled')
            buy_yes = getattr(strategy, 'total_buy_yes', 0)
            buy_no = getattr(strategy, 'total_buy_no', 0)
            holds = getattr(strategy, 'total_holds', 0)
            total_d = getattr(strategy, 'total_decisions', 0)
            executed = getattr(strategy, 'total_executed', 0)
            event_count = getattr(engine, '_event_count', 0)
            ob_count = len(trader.market_data.order_books)
            news_buf = getattr(strategy, 'news_buffer_count', 0)

            lines = [
                '[bold cyan]Statistics[/bold cyan]',
                f'Runtime:        {runtime}',
                f'Events:         {event_count}',
                f'Order Books:    {ob_count}',
                f'News Buffered:  {news_buf}',
                f'Orders:         {len(orders)} ({filled} filled)',
                f'LLM Decisions:  {total_d}',
                f'  YES/NO/HOLD:  {buy_yes}/{buy_no}/{holds}',
                f'  Executed:     {executed}',
            ]
            self.update('\n'.join(lines))
        except Exception:
            pass


class LLMDecisionsTable(DataTable):
    """LLM decision history — scrollable DataTable with cursor navigation."""

    DEFAULT_CSS = """
    LLMDecisionsTable {
        border: solid magenta;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def on_mount(self) -> None:
        self.cursor_type = 'row'
        self.zebra_stripes = True
        self.add_columns(
            'Time', 'Action', 'LLM', 'Mkt', 'Edge', 'Market', 'Reasoning', '✓'
        )

    def refresh_data(self, strategy) -> None:
        decisions = list(getattr(strategy, 'decisions', []))
        if len(decisions) == self._last_len:
            return  # Nothing new

        # Save cursor position and restore after re-render
        saved_row = self.cursor_row
        self.clear()
        for d in reversed(decisions[-40:]):
            action_style = {
                'BUY_YES': 'bold green',
                'BUY_NO': 'bold red',
                'HOLD': 'dim',
                'CLOSE_EDGE_TP': 'bold yellow',
                'CLOSE_EDGE_REV': 'bold red',
                'CLOSE_REEVAL': 'bold magenta',
                'CLOSE_TIMEOUT': 'yellow',
            }.get(d.action, 'white')

            llm = getattr(d, 'llm_prob', 0.0) or 0.0
            mkt = getattr(d, 'market_price', 0.0) or 0.0
            edge = llm - mkt
            self.add_row(
                d.timestamp,
                Text(d.action, style=action_style),
                f'{llm:.0%}' if llm > 0 else '—',
                f'{mkt:.0%}' if mkt > 0 else '—',
                f'{edge:+.0%}' if mkt > 0 else '—',
                (d.ticker_name or '')[:22],
                Text((getattr(d, 'reasoning', '') or '')[:40], style='dim'),
                Text('✓', style='bold green') if d.executed else Text('—', style='dim'),
            )

        self._last_len = len(decisions)
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass


class TradingPanel(Static):
    """Positions and recent orders (scrollable Static)."""

    DEFAULT_CSS = """
    TradingPanel {
        border: solid yellow;
        padding: 0 1;
        height: 1fr;
        overflow-y: scroll;
    }
    """

    def refresh_data(self, trader, position_manager) -> None:
        try:
            lines = [
                '[bold yellow]Trading Activity[/bold yellow]',
                '[dim]── Positions ──[/dim]',
            ]
            positions = [
                p for p in position_manager.get_non_cash_positions() if p.quantity > 0
            ]
            if positions:
                md = trader.market_data
                for p in positions:
                    bid = md.get_best_bid(p.ticker)
                    cur = bid.price if bid else Decimal('0')
                    pnl = (
                        (cur - p.average_cost) * p.quantity if cur > 0 else Decimal('0')
                    )
                    style = 'green' if pnl >= 0 else 'red'
                    name = (getattr(p.ticker, 'name', '') or p.ticker.symbol)[:28]
                    lines.append(
                        f'  [{style}]{name}[/{style}]'
                        f' qty={p.quantity} cost=${p.average_cost:.4f}'
                        f' pnl=[{style}]${pnl:+.2f}[/{style}]'
                    )
            else:
                lines.append('  [dim]No positions yet[/dim]')

            lines += ['', '[dim]── Recent Orders ──[/dim]']
            orders = list(getattr(trader, 'orders', []))[-8:]
            if orders:
                for o in reversed(orders):
                    side_c = 'green' if o.side.value == 'buy' else 'red'
                    name = (getattr(o.ticker, 'name', '') or o.ticker.symbol)[:26]
                    lines.append(
                        f'  [{side_c}]{o.side.value.upper()}[/{side_c}]'
                        f' {name} ${o.limit_price:.4f}'
                        f' [{o.status.value.upper()}]'
                    )
            else:
                lines.append('  [dim]No orders yet[/dim]')
            self.update('\n'.join(lines))
        except Exception:
            pass


class ActivityLog(RichLog):
    """Scrollable activity log — appends new entries incrementally."""

    DEFAULT_CSS = """
    ActivityLog {
        border: solid cyan;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def refresh_data(self, engine) -> None:
        try:
            log: list = list(getattr(engine, '_activity_log', []))
            new_entries = log[self._last_len :]
            for ts, msg in new_entries:
                style = (
                    'green'
                    if 'BUY' in msg
                    else 'red'
                    if 'SELL' in msg or 'Error' in msg
                    else 'bright_magenta'
                    if 'LLM' in msg
                    else 'cyan'
                    if 'News' in msg
                    else 'white'
                )
                self.write(
                    Text.from_markup(f'[dim]{ts}[/dim] [{style}]{msg}[/{style}]')
                )
            self._last_len = len(log)
        except Exception:
            pass


class OrderBooksTable(DataTable):
    """Live order books — scrollable DataTable."""

    DEFAULT_CSS = """
    OrderBooksTable {
        border: solid green;
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.cursor_type = 'row'
        self.zebra_stripes = True
        self.add_columns('Market', 'Bid', 'Ask', 'Sprd', 'Mid')

    def refresh_data(self, trader) -> None:
        from swm_agent.ticker.ticker import CashTicker

        try:
            ob_items = list(trader.market_data.order_books.items())
        except RuntimeError:
            return

        active = []
        for ticker, ob in ob_items:
            if isinstance(ticker, CashTicker):
                continue
            bid, ask = ob.best_bid, ob.best_ask
            if not (bid and ask and bid.price > 0):
                continue
            mid = (bid.price + ask.price) / 2
            if mid < Decimal('0.05') or mid > Decimal('0.95'):
                continue
            active.append((ticker, bid, ask, ask.price - bid.price, mid))

        active.sort(key=lambda x: abs(x[4] - Decimal('0.5')))

        saved_row = self.cursor_row
        self.clear()
        for ticker, bid, ask, spread, mid in active[:40]:
            name = (getattr(ticker, 'name', '') or ticker.symbol)[:32]
            sp_style = (
                'green'
                if spread <= Decimal('0.02')
                else 'yellow'
                if spread <= Decimal('0.05')
                else 'red'
            )
            self.add_row(
                name,
                f'${bid.price:.4f}',
                f'${ask.price:.4f}',
                Text(f'{spread:.4f}', style=sp_style),
                f'{mid * 100:.0f}%',
            )
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass


class NewsLog(RichLog):
    """Scrollable news feed — appends new headlines incrementally."""

    DEFAULT_CSS = """
    NewsLog {
        border: solid yellow;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def refresh_data(self, engine) -> None:
        try:
            news: list = list(getattr(engine, '_news', []))
            for ts, headline in news[self._last_len :]:
                self.write(Text.from_markup(f'[dim]{ts}[/dim] {headline}'))
            self._last_len = len(news)
        except Exception:
            pass


# ── Main App ───────────────────────────────────────────────────────────


class TradingMonitorApp(App[None]):
    """Full-screen interactive trading monitor powered by Textual."""

    CSS = """
    Screen {
        background: #1a1b26;
        layout: vertical;
    }
    #top-row {
        height: 2fr;
    }
    #mid-row, #bot-row {
        height: 1fr;
    }
    #top-row, #mid-row, #bot-row {
        layout: horizontal;
    }
    #left-col {
        width: 34;
        layout: vertical;
    }
    LLMDecisionsTable, ActivityLog, OrderBooksTable, NewsLog, TradingPanel {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding('q', 'quit', 'Quit'),
        Binding('p', 'toggle_pause', 'Pause'),
        Binding('tab', 'focus_next', 'Next Panel', show=True),
        Binding('shift+tab', 'focus_previous', 'Prev Panel', show=True),
        Binding('j', 'scroll_down_panel', 'Scroll ↓', show=False),
        Binding('k', 'scroll_up_panel', 'Scroll ↑', show=False),
    ]

    def __init__(self, engine: 'TradingEngine', exchange_name: str = '') -> None:
        super().__init__()
        self.engine = engine
        self.exchange_name = exchange_name
        self._paused = False
        self._monitor_start = datetime.now()

    def compose(self) -> ComposeResult:
        title = (
            f'SWM Agent — {self.exchange_name} Trading Monitor'
            if self.exchange_name
            else 'SWM Agent Trading Monitor'
        )
        yield Header(show_clock=True)
        with Horizontal(id='top-row'):
            with Vertical(id='left-col'):
                yield PortfolioPanel(id='portfolio')
                yield StatsPanel(self._monitor_start, id='stats')
            yield LLMDecisionsTable(id='llm')
        with Horizontal(id='mid-row'):
            yield TradingPanel(id='trading')
            yield ActivityLog(id='activity', highlight=True, markup=True)
        with Horizontal(id='bot-row'):
            yield OrderBooksTable(id='orderbooks')
            yield NewsLog(id='news', highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = (
            f'SWM Agent — {self.exchange_name}' if self.exchange_name else 'SWM Agent'
        )
        # Start the trading engine as a Textual worker so it runs inside
        # Textual's event loop (main thread), avoiding signal handler errors.
        self.run_worker(self._run_engine(), exclusive=True, exit_on_error=False)
        self.set_interval(2.0, self._refresh_all)

    async def _run_engine(self) -> None:
        """Run the trading engine within Textual's asyncio event loop."""
        try:
            await self.engine.start()
        except Exception as exc:
            logger.error('Engine worker error: %s', exc, exc_info=True)
            self.notify(f'Engine error: {exc}', severity='error', timeout=10)

    def _refresh_all(self) -> None:
        """Sync data from the trading engine into all widgets."""
        if self._paused:
            return
        try:
            engine = self.engine
            strategy = engine.strategy
            trader = engine.trader
            pm = trader.position_manager

            self.query_one('#portfolio', PortfolioPanel).refresh_data(trader, pm)
            self.query_one('#stats', StatsPanel).refresh_data(
                trader, pm, strategy, engine
            )
            self.query_one('#llm', LLMDecisionsTable).refresh_data(strategy)
            self.query_one('#trading', TradingPanel).refresh_data(trader, pm)
            self.query_one('#activity', ActivityLog).refresh_data(engine)
            self.query_one('#orderbooks', OrderBooksTable).refresh_data(trader)
            self.query_one('#news', NewsLog).refresh_data(engine)
        except Exception as e:
            logger.debug('Monitor refresh error: %s', e)

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        msg = '⏸  Paused — press [p] to resume' if self._paused else '▶  Resumed'
        self.notify(msg, timeout=3)

    def action_scroll_down_panel(self) -> None:
        """j — scroll down in the focused widget."""
        focused = self.focused
        if focused is not None:
            focused.scroll_down()

    def action_scroll_up_panel(self) -> None:
        """k — scroll up in the focused widget."""
        focused = self.focused
        if focused is not None:
            focused.scroll_up()

    def action_quit(self) -> None:
        """q — exit the app (worker and engine stop automatically)."""
        self.exit()
