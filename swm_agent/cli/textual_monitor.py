"""Textual-based interactive trading monitor for SWM Agent.

Key bindings (native, zero-latency):
  q / Ctrl+C  — quit
  s           — emergency stop (double-press confirm)
  Tab         — focus next panel
  Shift+Tab   — focus previous panel
  Arrow keys  — scroll / navigate within focused panel
  j / k       — scroll down / up within focused panel
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Static

if TYPE_CHECKING:
    from swm_agent.cli.control import ControlServer
    from swm_agent.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────


def _fmt_pnl(v: Decimal) -> str:
    if v > 0:
        return f'[green]+${v:.2f}[/green]'
    if v < 0:
        return f'[red]-${abs(v):.2f}[/red]'
    return f'${v:.2f}'


# ── Control bar ────────────────────────────────────────────────────────


class ControlBar(Horizontal):
    """Bottom control bar: status indicator + Pause / Resume / Stop buttons.

    Human operators click buttons; the agent uses ``swm-agent trade`` CLI.
    Both paths talk to the same Unix socket — identical effect.
    """

    DEFAULT_CSS = """
    ControlBar {
        height: 3;
        align: center middle;
        background: #1e1e30;
        border-top: solid #3a3a5a;
        padding: 0 2;
    }
    ControlBar #ctrl-status {
        width: 1fr;
        content-align: left middle;
        padding: 0 1;
    }
    ControlBar Button {
        margin: 0 1;
        min-width: 16;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label('● Connecting…', id='ctrl-status')
        yield Button('⏸  Pause', id='btn-pause', variant='warning')
        yield Button('▶  Resume', id='btn-resume', variant='success', disabled=True)
        yield Button('⏹  E-Stop', id='btn-stop', variant='error')

    def update_state(self, paused: bool, connected: bool = True) -> None:
        """Update button states and status label to match engine state."""
        lbl = self.query_one('#ctrl-status', Label)
        btn_pause = self.query_one('#btn-pause', Button)
        btn_resume = self.query_one('#btn-resume', Button)
        btn_stop = self.query_one('#btn-stop', Button)

        btn_stop.disabled = not connected

        if not connected:
            lbl.update('⚠  Engine not connected')
            btn_pause.disabled = True
            btn_resume.disabled = True
            return

        if paused:
            lbl.update('[bold yellow]⏸  Engine PAUSED[/bold yellow]')
            btn_pause.disabled = True
            btn_resume.disabled = False
        else:
            lbl.update('[bold green]●  Engine running[/bold green]')
            btn_pause.disabled = False
            btn_resume.disabled = True


# ── Panels ─────────────────────────────────────────────────────────────


class PortfolioPanel(Static):
    """Portfolio summary (Static text, updates in-place)."""

    DEFAULT_CSS = """
    PortfolioPanel {
        border: solid blue;
        border-title-color: blue;
        padding: 0 1;
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.border_title = '💼 Portfolio'

    def refresh_from_state(self, state: dict) -> None:
        p = state.get('portfolio', {})
        total = p.get('total', 0.0)
        realized = p.get('realized_pnl', 0.0)
        unrealized = p.get('unrealized_pnl', 0.0)
        lines = [
            '[bold blue]Portfolio Summary[/bold blue]',
            f'Total:          [bold]${total:>10.2f}[/bold]',
        ]
        for cp in p.get('cash_positions', []):
            lines.append(f"  {cp['symbol']:<18} ${cp['qty']:>8.2f}")
        lines += [
            f'Realized P&L:   {_fmt_pnl(Decimal(str(realized)))}',
            f'Unrealized P&L: {_fmt_pnl(Decimal(str(unrealized)))}',
            f'Total P&L:      {_fmt_pnl(Decimal(str(realized + unrealized)))}',
        ]
        self.update('\n'.join(lines))

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
        border-title-color: cyan;
        padding: 0 1;
        height: 1fr;
    }
    """

    def __init__(self, start_time: datetime, **kwargs) -> None:
        super().__init__(**kwargs)
        self.start_time = start_time

    def on_mount(self) -> None:
        self.border_title = '📊 Statistics'

    def refresh_from_state(self, state: dict) -> None:
        s = state.get('stats', {})
        lines = [
            '[bold cyan]Statistics[/bold cyan]',
            f"Runtime:        {state.get('runtime', '—')}",
            f"Events:         {s.get('event_count', 0)}",
            f"Order Books:    {s.get('order_books', 0)}",
            f"News Buffered:  {s.get('news_buffered', 0)}",
            f"Orders:         {s.get('orders_total', 0)} ({s.get('orders_filled', 0)} filled)",
            f"LLM Decisions:  {s.get('decisions', 0)}",
            f"  YES/NO/HOLD:  {s.get('buy_yes', 0)}/{s.get('buy_no', 0)}/{s.get('holds', 0)}",
            f"  Executed:     {s.get('executed', 0)}",
        ]
        self.update('\n'.join(lines))

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
        border-title-color: magenta;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def on_mount(self) -> None:
        self.border_title = '🤖 LLM Decisions  [LLM% vs Market%]'
        self.cursor_type = 'row'
        self._initialized = True
        self.zebra_stripes = True
        self.add_columns(
            'Time', 'Action', 'LLM', 'Mkt', 'Edge', 'Market', 'Reasoning', '✓'
        )

    def refresh_from_state(self, decisions: list) -> None:
        if len(decisions) == self._last_len:
            return
        saved_row = self.cursor_row
        self.clear()
        for d in reversed(decisions[-40:]):
            action = d.get('action', '')
            action_style = {
                'BUY_YES': 'bold green',
                'BUY_NO': 'bold red',
                'HOLD': 'dim',
                'CLOSE_EDGE_TP': 'bold yellow',
                'CLOSE_EDGE_REV': 'bold red',
                'CLOSE_REEVAL': 'bold magenta',
                'CLOSE_TIMEOUT': 'yellow',
            }.get(action, 'white')
            llm = d.get('llm_prob', 0.0)
            mkt = d.get('market_price', 0.0)
            edge = llm - mkt
            self.add_row(
                d.get('timestamp', ''),
                Text(action, style=action_style),
                f'{llm:.0%}' if llm > 0 else '—',
                f'{mkt:.0%}' if mkt > 0 else '—',
                f'{edge:+.0%}' if mkt > 0 else '—',
                d.get('ticker_name', '')[:22],
                Text(d.get('reasoning', '')[:40], style='dim'),
                Text('✓', style='bold green')
                if d.get('executed')
                else Text('—', style='dim'),
            )
        self._last_len = len(decisions)
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass

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
        border-title-color: yellow;
        padding: 0 1;
        height: 1fr;
        overflow-y: scroll;
    }
    """

    def on_mount(self) -> None:
        self.border_title = '📈 Positions & Orders'

    def refresh_from_state(self, state: dict) -> None:
        lines = [
            '[bold yellow]Trading Activity[/bold yellow]',
            '[dim]── Positions ──[/dim]',
        ]
        positions = state.get('positions', [])
        if positions:
            for p in positions:
                pnl_val = float(p.get('pnl', '0').replace('+', ''))
                style = 'green' if pnl_val >= 0 else 'red'
                lines.append(
                    f"  [{style}]{p['name']}[/{style}]"
                    f" qty={p['qty']} cost=${p['avg_cost']}"
                    f" pnl=[{style}]{p['pnl']}[/{style}]"
                )
        else:
            lines.append('  [dim]No positions yet[/dim]')
        lines += ['', '[dim]── Recent Orders ──[/dim]']
        orders = state.get('orders', [])
        if orders:
            for o in reversed(orders):
                side_c = 'green' if o['side'] == 'buy' else 'red'
                lines.append(
                    f"  [{side_c}]{o['side'].upper()}[/{side_c}]"
                    f" {o['name']} ${o['limit_price']}"
                    f" [{o['status'].upper()}]"
                )
        else:
            lines.append('  [dim]No orders yet[/dim]')
        self.update('\n'.join(lines))

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
        border-title-color: cyan;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def on_mount(self) -> None:
        self.border_title = '📋 Activity Log'

    def refresh_from_state(self, log: list) -> None:
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
            self.write(Text.from_markup(f'[dim]{ts}[/dim] [{style}]{msg}[/{style}]'))
        self._last_len = len(log)

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
        border-title-color: green;
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.border_title = '📖 Order Books  [sorted by distance from 50%]'
        self.cursor_type = 'row'
        self._socket_mode = False  # set True by SocketTradingMonitorApp
        self.zebra_stripes = True
        self.add_columns('Market', 'Bid', 'Ask', 'Sprd', 'Mid')

    def refresh_from_state(self, books: list) -> None:
        saved_row = self.cursor_row
        self.clear()
        for b in books:
            spread = float(b['spread'])
            sp_style = (
                'green' if spread <= 0.02 else 'yellow' if spread <= 0.05 else 'red'
            )
            self.add_row(
                b['name'],
                f"${b['bid']}",
                f"${b['ask']}",
                Text(b['spread'], style=sp_style),
                f"{b['mid']}%",
            )
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass

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
        border-title-color: yellow;
        height: 1fr;
    }
    """

    _last_len: int = 0

    def on_mount(self) -> None:
        self.border_title = '📰 News Headlines'

    def refresh_from_state(self, news: list) -> None:
        for ts, headline in news[self._last_len :]:
            self.write(Text.from_markup(f'[dim]{ts}[/dim] {headline}'))
        self._last_len = len(news)

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

    # Monitor is read-only for keyboard — buttons and swm-agent trade CLI control engine.
    BINDINGS = [
        Binding('q', 'quit', 'Close Monitor', show=True),
        Binding('s', 'estop', 'E-Stop', show=True),
        Binding('tab', 'focus_next', 'Next Panel', show=True),
        Binding('shift+tab', 'focus_previous', 'Prev Panel', show=True),
        Binding('j', 'scroll_down_panel', 'Scroll ↓', show=False),
        Binding('k', 'scroll_up_panel', 'Scroll ↑', show=False),
    ]

    def __init__(
        self,
        engine: TradingEngine,
        exchange_name: str = '',
        control_server: ControlServer | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.exchange_name = exchange_name
        self.control_server = control_server
        self._monitor_start = datetime.now()
        self._stop_armed: bool = False  # two-click confirmation for E-Stop

    def compose(self) -> ComposeResult:
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
        yield ControlBar(id='ctrl-bar')
        yield Footer()

    def on_mount(self) -> None:
        self.title = (
            f'SWM Agent — {self.exchange_name}' if self.exchange_name else 'SWM Agent'
        )
        # Start the trading engine as a Textual worker so it runs inside
        # Textual's event loop (main thread), avoiding signal handler errors.
        self.run_worker(self._run_engine(), exclusive=True, exit_on_error=False)
        self.set_interval(2.0, self._refresh_all)
        self.set_interval(0.5, self._refresh_title)  # low-latency paused indicator

    async def _run_engine(self) -> None:
        """Run the control server + trading engine within Textual's event loop."""
        try:
            if self.control_server is not None:
                await self.control_server.start()
            await self.engine.start()
        except Exception as exc:
            logger.error('Engine worker error: %s', exc, exc_info=True)
            self.notify(f'Engine error: {exc}', severity='error', timeout=10)
        finally:
            if self.control_server is not None:
                await self.control_server.stop()

    def _refresh_title(self) -> None:
        """Update subtitle and control-bar buttons to reflect engine state."""
        if self.control_server is None:
            return
        paused = self.control_server.paused
        activity_log = list(getattr(self.engine, '_activity_log', []))
        last_activity = activity_log[-1][1] if activity_log else 'No activity yet'
        self.sub_title = (
            '⏸  PAUSED — click ▶ Resume or: swm-agent trade resume'
            if paused
            else '▶  Running — click ⏸ Pause or: swm-agent trade pause'
        )
        self.sub_title = (
            f'{self.sub_title}  |  Last: {last_activity[:60]}  |  E-Stop: s'
        )
        try:
            self.query_one('#ctrl-bar', ControlBar).update_state(paused, connected=True)
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle control-bar button clicks (same effect as swm-agent trade CLI)."""
        btn_id = event.button.id

        if btn_id == 'btn-pause' and self.control_server:
            self.control_server._cmd_pause()
            self._refresh_title()
            self.notify('⏸  Engine paused', severity='warning', timeout=3)

        elif btn_id == 'btn-resume' and self.control_server:
            self.control_server._cmd_resume()
            self._refresh_title()
            self.notify('▶  Engine resumed', timeout=2)

        elif btn_id == 'btn-stop':
            if not self._stop_armed:
                self._stop_armed = True
                self.notify(
                    '⚠  Click ⏹ E-Stop again within 3 s to confirm',
                    severity='warning',
                    timeout=3,
                )
                self.set_timer(3.0, self._disarm_stop)
            else:
                self._stop_armed = False
                self.notify('⏹  Stopping engine…', severity='error', timeout=5)
                self.exit()  # worker cancels → engine.stop() called in finally

    def _disarm_stop(self) -> None:
        self._stop_armed = False

    def _refresh_all(self) -> None:
        """Sync data from the trading engine into all widgets."""
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

    def action_estop(self) -> None:
        """s — keyboard emergency stop (same two-step guard as button)."""
        if not self._stop_armed:
            self._stop_armed = True
            self.notify(
                '⚠  Press s again within 3 s to confirm emergency stop',
                severity='warning',
                timeout=3,
            )
            self.set_timer(3.0, self._disarm_stop)
            return
        self._stop_armed = False
        self.notify('⏹  Stopping engine…', severity='error', timeout=5)
        self.exit()


# ── Standalone socket monitor (independent process) ────────────────────


class SocketTradingMonitorApp(App[None]):
    """Read-only monitor that connects to a running engine via Unix socket.

    Runs in a completely separate process. The engine continues unaffected
    when this app is closed.

    Start with:  swm-agent monitor
    """

    CSS = TradingMonitorApp.CSS  # reuse identical layout

    BINDINGS = [
        Binding('q', 'quit', 'Close Monitor', show=True),
        Binding('s', 'e_stop', 'E-Stop', show=True),
        Binding('tab', 'focus_next', 'Next Panel', show=True),
        Binding('shift+tab', 'focus_previous', 'Prev Panel', show=True),
        Binding('j', 'scroll_down_panel', 'Scroll ↓', show=False),
        Binding('k', 'scroll_up_panel', 'Scroll ↑', show=False),
    ]

    def __init__(self, socket_path: Path | None = None) -> None:
        from swm_agent.cli.control import SOCKET_PATH

        super().__init__()
        self.socket_path = socket_path or SOCKET_PATH
        self._monitor_start = datetime.now()
        self._connected: bool = False
        self._paused: bool = False
        self._stop_armed: bool = False  # two-click confirmation for E-Stop

    def compose(self) -> ComposeResult:
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
        yield ControlBar(id='ctrl-bar')
        yield Footer()

    def on_mount(self) -> None:
        self.title = 'SWM Agent — Socket Monitor'
        self.sub_title = f'Connecting to {self.socket_path} …'
        self.set_interval(2.0, self._poll_state)

    async def _poll_state(self) -> None:
        """Fetch state from the engine via socket and refresh all widgets."""
        from swm_agent.cli.control import send_command

        try:
            state = await send_command('get_state', socket_path=self.socket_path)
        except FileNotFoundError:
            self._connected = False
            self.sub_title = f'⚠  Engine not running ({self.socket_path})'
            return
        except Exception as exc:
            self._connected = False
            self.sub_title = f'⚠  Connection error: {exc}'
            return

        self._connected = True
        self._paused = state.get('paused', False)
        self.sub_title = (
            '⏸  Engine PAUSED — click ▶ Resume or: swm-agent trade resume'
            if self._paused
            else '▶  Engine running — click ⏸ Pause or: swm-agent trade pause'
        )
        last_activity = state.get('activity_log') or []
        last_msg = last_activity[-1][1] if last_activity else 'No activity yet'
        self.sub_title = f'{self.sub_title}  |  Last: {last_msg[:60]}  |  E-Stop: s'

        try:
            ctrl = self.query_one('#ctrl-bar', ControlBar)
            ctrl.update_state(self._paused, connected=True)
        except Exception:
            pass

        try:
            self.query_one('#portfolio', PortfolioPanel).refresh_from_state(state)
            self.query_one('#stats', StatsPanel).refresh_from_state(state)
            self.query_one('#llm', LLMDecisionsTable).refresh_from_state(
                state.get('llm_decisions', [])
            )
            self.query_one('#trading', TradingPanel).refresh_from_state(state)
            self.query_one('#activity', ActivityLog).refresh_from_state(
                state.get('activity_log', [])
            )
            self.query_one('#orderbooks', OrderBooksTable).refresh_from_state(
                state.get('order_books', [])
            )
            self.query_one('#news', NewsLog).refresh_from_state(state.get('news', []))
        except Exception as exc:
            logger.debug('Socket monitor render error: %s', exc)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle control-bar button clicks (same effect as swm-agent trade CLI)."""
        from swm_agent.cli.control import send_command

        btn_id = event.button.id

        if btn_id == 'btn-pause':
            try:
                await send_command('pause', socket_path=self.socket_path)
                self.notify('⏸  Engine paused', severity='warning', timeout=3)
            except Exception as exc:
                self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

        elif btn_id == 'btn-resume':
            try:
                await send_command('resume', socket_path=self.socket_path)
                self.notify('▶  Engine resumed', timeout=2)
            except Exception as exc:
                self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

        elif btn_id == 'btn-stop':
            if not self._stop_armed:
                self._stop_armed = True
                self.notify(
                    '⚠  Click ⏹ E-Stop again within 3 s to confirm emergency stop',
                    severity='warning',
                    timeout=3,
                )
                self.set_timer(3.0, self._disarm_stop)
            else:
                self._stop_armed = False
                try:
                    await send_command('stop', socket_path=self.socket_path)
                    self.notify('⏹  Stop signal sent', severity='error', timeout=4)
                except Exception as exc:
                    self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

    def _disarm_stop(self) -> None:
        self._stop_armed = False

    def action_scroll_down_panel(self) -> None:
        focused = self.focused
        if focused is not None:
            focused.scroll_down()

    def action_scroll_up_panel(self) -> None:
        focused = self.focused
        if focused is not None:
            focused.scroll_up()

    async def action_e_stop(self) -> None:
        """s — keyboard emergency stop over socket (same two-step guard)."""
        from swm_agent.cli.control import send_command

        if not self._stop_armed:
            self._stop_armed = True
            self.notify(
                '⚠  Press s again within 3 s to confirm emergency stop',
                severity='warning',
                timeout=3,
            )
            self.set_timer(3.0, self._disarm_stop)
            return

        self._stop_armed = False
        try:
            await send_command('stop', socket_path=self.socket_path)
            self.notify('⏹  Stop signal sent', severity='error', timeout=4)
        except Exception as exc:
            self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)
