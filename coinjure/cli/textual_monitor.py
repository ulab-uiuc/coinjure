"""Textual-based interactive trading monitor for Coinjure.

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
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Static

if TYPE_CHECKING:
    from coinjure.engine.control import ControlServer
    from coinjure.engine.engine import TradingEngine

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

    Human operators click buttons; the agent uses ``coinjure engine`` CLI.
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
        self.border_title = 'Portfolio'

    @staticmethod
    def _cash_lines(cash_positions: list) -> list[str]:
        """Format cash positions: header then one line per exchange."""
        lines = ['[bold]Cash[/bold]']
        if not cash_positions:
            lines.append('  —')
            return lines
        for cp in cash_positions:
            sym = cp['symbol'].replace('USDC_', '').replace('USD_', '')
            lines.append(f'  {sym:<10} ${cp["qty"]:.0f}')
        return lines

    @staticmethod
    def _fmt_return(pnl: Decimal, total: Decimal | float) -> str:
        """Format return percentage."""
        t = float(total) if not isinstance(total, float) else total
        if t <= 0:
            return ''
        pct = float(pnl) / t * 100
        if pct > 0:
            return f'  [green]({pct:+.2f}%)[/green]'
        if pct < 0:
            return f'  [red]({pct:+.2f}%)[/red]'
        return f'  (0.00%)'

    def refresh_from_state(self, state: dict) -> None:
        p = state.get('portfolio', {})
        total = p.get('total', 0.0)
        realized = Decimal(str(p.get('realized_pnl', 0.0)))
        unrealized = Decimal(str(p.get('unrealized_pnl', 0.0)))
        net_pnl = realized + unrealized
        ret = self._fmt_return(net_pnl, total)
        lines = [
            f'[bold]Value[/bold]     ${total:>10.2f}',
            *self._cash_lines(p.get('cash_positions', [])),
            f'Realized   {_fmt_pnl(realized)}',
            f'Unrealized {_fmt_pnl(unrealized)}',
            f'[bold]Net P&L[/bold]   {_fmt_pnl(net_pnl)}{ret}',
        ]
        self.update('\n'.join(lines))

    def refresh_data(self, trader, position_manager) -> None:
        try:
            md = trader.market_data
            pv = position_manager.get_portfolio_value(md)
            total = sum(pv.values(), Decimal('0'))
            realized = position_manager.get_total_realized_pnl()
            unrealized = position_manager.get_total_unrealized_pnl(md)
            net_pnl = realized + unrealized

            cash_positions = []
            for p in position_manager.get_cash_positions():
                sym = p.ticker.symbol.replace('USDC_', '').replace('USD_', '')
                cash_positions.append({'symbol': sym, 'qty': float(p.quantity)})
            ret = self._fmt_return(net_pnl, total)

            lines = [
                f'[bold]Value[/bold]     ${total:>10.2f}',
                *self._cash_lines(cash_positions),
                f'Realized   {_fmt_pnl(realized)}',
                f'Unrealized {_fmt_pnl(unrealized)}',
                f'[bold]Net P&L[/bold]   {_fmt_pnl(net_pnl)}{ret}',
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
        self.border_title = 'Statistics'

    @staticmethod
    def _fmt_decision_stats(ds: dict) -> str:
        """Format decision stats into a compact single line."""
        d = ds.get('decisions', 0)
        x = ds.get('executed', 0)
        by = ds.get('buy_yes', 0)
        bn = ds.get('buy_no', 0)
        cl = ds.get('closes', 0) + ds.get('sells', 0)
        return f'{d}d {x}x {by}↑ {bn}↓ {cl}✕'

    def refresh_from_state(self, state: dict) -> None:
        s = state.get('stats', {})
        ds = s.get('decision_stats', {})
        lines = [
            '[bold cyan]Stats[/bold cyan]',
            f"Run:  {state.get('runtime', '—')}  Ev: {s.get('event_count', 0)}",
            f"OB: {s.get('order_books', 0)}  News: {s.get('news_buffered', 0)}  Ord: {s.get('orders_total', 0)}({s.get('orders_filled', 0)}f)",
            f'Sig:  {self._fmt_decision_stats(ds)}',
        ]
        self.update('\n'.join(lines))

    def refresh_data(self, trader, position_manager, strategy, engine) -> None:
        try:
            runtime = str(datetime.now() - self.start_time).split('.')[0]
            orders = list(getattr(trader, 'orders', []))
            filled = sum(1 for o in orders if o.status.value == 'filled')
            ds = strategy.get_decision_stats()
            event_count = getattr(engine, '_event_count', 0)
            ob_count = len(trader.market_data.order_books)
            news_buf = max(
                int(getattr(strategy, 'news_buffer_count', 0) or 0),
                len(list(getattr(engine, '_news', []))),
            )

            lines = [
                '[bold cyan]Stats[/bold cyan]',
                f'Run:  {runtime}  Ev: {event_count}',
                f'OB: {ob_count}  News: {news_buf}  Ord: {len(orders)}({filled}f)',
                f'Sig:  {self._fmt_decision_stats(ds)}',
            ]
            self.update('\n'.join(lines))
        except Exception:
            pass


class DecisionsTable(DataTable):
    """Strategy decision history — scrollable DataTable with dynamic signals."""

    DEFAULT_CSS = """
    DecisionsTable {
        border: solid magenta;
        border-title-color: magenta;
        height: 1fr;
    }
    """

    _last_len: int = 0
    _last_ts: str = ''

    def on_mount(self) -> None:
        self.border_title = 'Strategy Decisions'
        self.cursor_type = 'row'
        self._initialized = True
        self.zebra_stripes = True
        self._current_signal_keys: list[str] = []
        self._set_columns([])

    # Short action labels for display
    _ACTION_DISPLAY: ClassVar[dict[str, tuple[str, str]]] = {
        # action -> (short_label, style)
        'BUY_YES': ('BUY Y', 'bold green'),
        'BUY_NO': ('BUY N', 'bold red'),
        'SELL_YES': ('SELL Y', 'bold red'),
        'SELL_NO': ('SELL N', 'bold green'),
        'ENTER_ARB': ('ENTER', 'bold green'),
        'EXIT_ARB': ('EXIT', 'bold yellow'),
        'BUY_SPREAD': ('BUY SP', 'bold green'),
        'SELL_SPREAD': ('SELL SP', 'bold red'),
        'CLOSE_SPREAD': ('CLS SP', 'bold yellow'),
        'CLOSE_BUY_YES': ('CLS Y', 'bold yellow'),
        'CLOSE_BUY_NO': ('CLS N', 'bold yellow'),
        'CLOSE_EDGE_TP': ('CLS TP', 'bold yellow'),
        'CLOSE_EDGE_REV': ('CLS REV', 'bold red'),
        'CLOSE_REEVAL': ('CLS RE', 'bold magenta'),
        'CLOSE_TIMEOUT': ('CLS TO', 'yellow'),
        'LONG_A': ('LONG', 'bold green'),
        'SHORT_A': ('SHORT', 'bold red'),
        'CLOSE_LONG_A': ('CLS LG', 'bold yellow'),
        'CLOSE_SHORT_A': ('CLS SH', 'bold yellow'),
        'BUY_FOLLOWER': ('BUY FL', 'bold green'),
        'SELL_FOLLOWER': ('SELL FL', 'bold red'),
        'EXIT_LONG': ('EXIT L', 'bold yellow'),
        'EXIT_SHORT': ('EXIT S', 'bold yellow'),
        'HOLD': ('HOLD', 'dim'),
    }

    @staticmethod
    def _fmt_signal(strategy_name: str, sig: dict) -> str:
        """Format signals compactly based on strategy type."""
        if not sig:
            return ''
        sn = strategy_name.lower()
        if 'implication' in sn:
            pa = sig.get('price_a')
            pb = sig.get('price_b')
            v = sig.get('violation')
            parts = []
            if pa is not None and pb is not None:
                parts.append(f'A={pa:.2f} B={pb:.2f}')
            if v is not None:
                parts.append(f'viol={v:+.3f}')
            return '  '.join(parts)
        if 'group' in sn:
            s = sig.get('sum_yes')
            e = sig.get('best_edge')
            parts = []
            if s is not None:
                parts.append(f'\u03a3={s:.2f}')
            if e is not None:
                parts.append(f'edge={e:+.3f}')
            lp = sig.get('leg_price')
            if lp is not None:
                parts.append(f'px={lp:.3f}')
            return '  '.join(parts)
        if 'coint' in sn or 'spread' in sn:
            sp = sig.get('spread')
            dv = sig.get('deviation')
            parts = []
            if sp is not None:
                parts.append(f'sprd={sp:.3f}')
            if dv is not None:
                parts.append(f'dev={dv:+.3f}')
            return '  '.join(parts)
        if 'conditional' in sn:
            pa = sig.get('price_a')
            lo = sig.get('lower')
            up = sig.get('upper')
            parts = []
            if pa is not None:
                parts.append(f'A={pa:.3f}')
            if lo is not None and up is not None:
                parts.append(f'[{lo:.2f},{up:.2f}]')
            return '  '.join(parts)
        if 'structural' in sn:
            r = sig.get('residual')
            pa = sig.get('price_a')
            parts = []
            if pa is not None:
                parts.append(f'A={pa:.3f}')
            if r is not None:
                parts.append(f'res={r:+.3f}')
            return '  '.join(parts)
        if 'lead_lag' in sn or 'lag' in sn:
            lm = sig.get('leader_move')
            parts = []
            if lm is not None:
                parts.append(f'move={lm:+.3f}')
            return '  '.join(parts)
        # Fallback: first 2 signal values
        items = list(sig.items())[:2]
        return '  '.join(
            f'{k[:6]}={v:.3f}' for k, v in items if isinstance(v, (int, float))
        )

    def _set_columns(self, multi: bool = False) -> None:
        self.clear(columns=True)
        mid_col = 'Relation' if multi else 'Market'
        self.add_columns('Time', 'Type', 'Action', mid_col, 'Reason')
        self._multi_engine = multi

    @staticmethod
    def _build_reason(strategy_name: str, d: dict) -> str:
        """Build a compact reason string from signals + reasoning."""
        sig = d.get('signal_values', {}) or {}
        reason = d.get('reasoning', '') or ''
        sig_str = DecisionsTable._fmt_signal(strategy_name, sig)
        if sig_str and reason:
            return f'{sig_str} | {reason}'
        return sig_str or reason

    def refresh_from_state(self, decisions: list, multi: bool = False) -> None:
        multi_changed = multi != getattr(self, '_multi_engine', False)
        latest_ts = decisions[-1].get('timestamp', '') if decisions else ''
        if (
            latest_ts == self._last_ts
            and len(decisions) == self._last_len
            and not multi_changed
        ):
            return
        if multi_changed or not hasattr(self, '_multi_engine'):
            self._set_columns(multi=multi)
        saved_row = self.cursor_row
        self.clear()
        # Show executed decisions first, then fill remaining slots with HOLDs
        executed = [d for d in decisions if d.get('executed')]
        holds = [d for d in decisions if not d.get('executed')]
        # Show up to 30 executed + up to 10 recent HOLDs
        to_show = executed[-30:] + holds[-10:]
        to_show.sort(key=lambda d: d.get('timestamp', ''))
        for d in reversed(to_show[-40:]):
            action = d.get('action', '')
            label, style = self._ACTION_DISPLAY.get(action, (action[:8], 'white'))
            sname = d.get('strategy_name', '')
            reason = self._build_reason(sname, d)
            ex = d.get('executed')
            reason_style = 'dim' if not ex else ''
            self.add_row(
                d.get('timestamp', ''),
                Text(sname[:10], style='dim cyan'),
                Text(label, style=style if ex else 'dim'),
                d.get('ticker_name', '')[:22],
                Text(reason[:50], style=reason_style),
            )
        self._last_len = len(decisions)
        self._last_ts = latest_ts
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass

    def refresh_data(self, strategy) -> None:
        decisions = list(strategy.get_decisions())
        strategy_name = getattr(strategy, 'name', '') or ''
        if len(decisions) == self._last_len:
            return
        if not hasattr(self, '_multi_engine'):
            self._set_columns()

        saved_row = self.cursor_row
        self.clear()
        # Prioritize executed decisions over HOLDs
        executed = [d for d in decisions if d.executed]
        holds = [d for d in decisions if not d.executed]
        to_show = executed[-30:] + holds[-10:]
        to_show.sort(key=lambda d: d.timestamp or '')
        for d in reversed(to_show[-40:]):
            label, style = self._ACTION_DISPLAY.get(d.action, (d.action[:8], 'white'))
            sig = getattr(d, 'signal_values', {}) or {}
            sig_str = self._fmt_signal(strategy_name, sig)
            reason = getattr(d, 'reasoning', '') or ''
            if sig_str and reason:
                full_reason = f'{sig_str} | {reason}'
            else:
                full_reason = sig_str or reason
            reason_style = 'dim' if not d.executed else ''
            self.add_row(
                d.timestamp,
                Text(strategy_name[:10], style='dim cyan'),
                Text(label, style=style if d.executed else 'dim'),
                (d.ticker_name or '')[:22],
                Text(full_reason[:50], style=reason_style),
            )

        self._last_len = len(decisions)
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass


class PositionsPanel(DataTable):
    """Current positions table (scrollable DataTable)."""

    can_focus = True

    DEFAULT_CSS = """
    PositionsPanel {
        border: solid yellow;
        border-title-color: yellow;
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.border_title = 'Current Positions'
        self.cursor_type = 'row'
        self.zebra_stripes = True
        self.add_columns('Side', 'Market', 'Qty', 'Cost', 'Mark', 'P&L')

    @staticmethod
    def _fmt_qty(value: object) -> str:
        try:
            q = Decimal(str(value))
        except Exception:
            return str(value)
        s = f'{q:.2f}'.rstrip('0').rstrip('.')
        if '.' not in s:
            s += '.0'
        return s

    def refresh_from_state(self, state: dict) -> None:
        saved_row = self.cursor_row
        self.clear()
        positions = state.get('positions', [])
        if positions:
            for p in positions:
                pnl_raw = str(p.get('pnl', '0'))
                try:
                    pnl_val = float(pnl_raw.replace('+', ''))
                except Exception:
                    pnl_val = 0.0
                style = 'green' if pnl_val >= 0 else 'red'
                yn = p.get('side', '') or p.get('yn', '')
                yn_style = 'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
                self.add_row(
                    Text(yn, style=yn_style),
                    str(p.get('name', ''))[:30],
                    self._fmt_qty(p.get('qty', '')),
                    f"${str(p.get('avg_cost', '0'))[:7]}",
                    f"${str(p.get('mark', p.get('bid', '0')))[:7]}",
                    Text(pnl_raw, style=style),
                )
        else:
            self.add_row('—', Text('No positions yet', style='dim'), '—', '—', '—', '—')
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass

    def refresh_data(self, trader, position_manager) -> None:
        try:
            saved_row = self.cursor_row
            self.clear()
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
                    yn = (getattr(p.ticker, 'side', '') or '').upper()
                    yn_style = (
                        'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
                    )
                    name = (getattr(p.ticker, 'name', '') or p.ticker.symbol)[:30]
                    self.add_row(
                        Text(yn, style=yn_style),
                        name,
                        self._fmt_qty(p.quantity),
                        f'${p.average_cost:.4f}',
                        f'${cur:.4f}' if cur > 0 else '—',
                        Text(f'${pnl:+.2f}', style=style),
                    )
            else:
                self.add_row(
                    Text('No positions yet', style='dim'),
                    '—',
                    '—',
                    '—',
                    Text('—', style='dim'),
                )
            try:
                self.move_cursor(row=min(saved_row, self.row_count - 1))
            except Exception:
                pass
        except Exception:
            pass


class OrdersPanel(DataTable):
    """Current / recent orders table (scrollable DataTable)."""

    can_focus = True

    DEFAULT_CSS = """
    OrdersPanel {
        border: solid yellow;
        border-title-color: yellow;
        height: 1fr;
    }
    """

    _last_max_idx: int = -1

    def on_mount(self) -> None:
        self.border_title = 'Orders'
        self.cursor_type = 'row'
        self.zebra_stripes = True
        self.add_columns('#', 'Side', 'Y/N', 'Market', 'Px', 'Status')

    def refresh_from_state(self, state: dict) -> None:
        orders = state.get('orders', [])
        max_idx = max((o.get('idx', 0) for o in orders), default=-1)
        if max_idx == self._last_max_idx:
            return
        self._last_max_idx = max_idx
        saved_row = self.cursor_row
        self.clear()
        if orders:
            for o in reversed(orders):
                side = str(o.get('side', '')).lower()
                side_c = 'green' if side == 'buy' else 'red'
                yn = o.get('yn', '')
                yn_style = 'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
                st = str(o.get('status', '')).upper()
                st_style = (
                    'green'
                    if st == 'FILLED'
                    else 'yellow'
                    if st == 'PENDING'
                    else 'red'
                )
                self.add_row(
                    Text(str(o.get('idx', '')), style='dim'),
                    Text(side.upper(), style=side_c),
                    Text(yn, style=yn_style),
                    str(o.get('name', ''))[:28],
                    f"${o.get('limit_price', '-')}",
                    Text(st, style=st_style),
                )
        else:
            self.add_row(
                '—',
                Text('—', style='dim'),
                '—',
                Text('No orders yet', style='dim'),
                '—',
                '—',
            )
        try:
            self.move_cursor(row=min(saved_row, self.row_count - 1))
        except Exception:
            pass

    def refresh_data(self, trader) -> None:
        try:
            saved_row = self.cursor_row
            self.clear()
            orders = list(getattr(trader, 'orders', []))
            if orders:
                total = len(orders)
                for i, o in enumerate(reversed(orders[-8:])):
                    idx = total - i
                    side = o.side.value.lower()
                    side_c = 'green' if side == 'buy' else 'red'
                    yn = (getattr(o.ticker, 'side', '') or '').upper()
                    yn_style = (
                        'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
                    )
                    name = (getattr(o.ticker, 'name', '') or o.ticker.symbol)[:28]
                    st = o.status.value.upper()
                    st_style = (
                        'green'
                        if st == 'FILLED'
                        else 'yellow'
                        if st == 'PENDING'
                        else 'red'
                    )
                    self.add_row(
                        Text(str(idx), style='dim'),
                        Text(side.upper(), style=side_c),
                        Text(yn, style=yn_style),
                        name,
                        f'${o.limit_price:.4f}',
                        Text(st, style=st_style),
                    )
            else:
                self.add_row(
                    Text('—', style='dim'),
                    '—',
                    Text('No orders yet', style='dim'),
                    '—',
                    '—',
                )
            try:
                self.move_cursor(row=min(saved_row, self.row_count - 1))
            except Exception:
                pass
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
        self.border_title = 'Activity Log'

    def refresh_from_state(self, log: list) -> None:
        # Build set of already-seen entries to handle reordered/merged lists
        if not hasattr(self, '_seen_keys'):
            self._seen_keys: set[tuple[str, str]] = set()
        for entry in log:
            ts = entry[0] if isinstance(entry, (list, tuple)) and entry else ''
            msg = (
                entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else ''
            )
            key = (str(ts), str(msg))
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
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
        self.border_title = 'Order Books  [sorted by distance from 50%]'
        self.cursor_type = 'row'
        self._socket_mode = False  # set True by SocketTradingMonitorApp
        self.zebra_stripes = True
        self.add_columns('Y/N', 'Market', 'Bid', 'Ask', 'Sprd', 'Mid')

    def refresh_from_state(self, books: list) -> None:
        saved_row = self.cursor_row
        self.clear()
        if not books:
            self.add_row('—', '(no order books yet)', '-', '-', '-', '-')
            return
        for b in books:
            spread = float(b['spread'])
            sp_style = (
                'green' if spread <= 0.02 else 'yellow' if spread <= 0.05 else 'red'
            )
            yn = b.get('yn', '')
            yn_style = 'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
            self.add_row(
                Text(yn, style=yn_style),
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
        from coinjure.ticker import CashTicker

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
            active.append((ticker, bid, ask, ask.price - bid.price, mid))

        active.sort(key=lambda x: abs(x[4] - Decimal('0.5')))

        saved_row = self.cursor_row
        self.clear()
        if not active:
            self.add_row('—', '(no order books yet)', '-', '-', '-', '-')
            return
        for ticker, bid, ask, spread, mid in active[:40]:
            yn = (getattr(ticker, 'side', '') or '').upper()
            yn_style = 'green' if yn == 'YES' else 'red' if yn == 'NO' else 'dim'
            name = (getattr(ticker, 'name', '') or ticker.symbol)[:30]
            sp_style = (
                'green'
                if spread <= Decimal('0.02')
                else 'yellow'
                if spread <= Decimal('0.05')
                else 'red'
            )
            self.add_row(
                Text(yn, style=yn_style),
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
        border: solid #ff8c00;
        border-title-color: #ff8c00;
        height: 1fr;
    }
    """

    _last_len: int = 0
    _excluded_sources: set[str] = {'polymarket', 'kalshi'}

    def on_mount(self) -> None:
        self.border_title = 'News Headlines'

    @staticmethod
    def _normalize_news_item(item: object) -> tuple[str, str, str, str]:
        if isinstance(item, dict):
            return (
                str(item.get('timestamp', '')),
                str(item.get('title', '')),
                str(item.get('source', '')),
                str(item.get('url', '')),
            )
        if isinstance(item, tuple | list) and len(item) >= 2:
            return (str(item[0]), str(item[1]), '', '')
        return ('', str(item), '', '')

    def _render_news_item(self, item: object) -> None:
        ts, title, source, url = self._normalize_news_item(item)
        title_text = title or '(no title)'
        meta = source if source else ''
        if url:
            meta = f'{meta} | {url}' if meta else url
        line = f'[dim]{ts}[/dim] {title_text}'
        if meta:
            line += f'\n[dim]{meta}[/dim]'
        self.write(Text.from_markup(line))

    def _should_render_news_item(self, item: object) -> bool:
        _, _, source, _ = self._normalize_news_item(item)
        return source.strip().lower() not in self._excluded_sources

    def refresh_from_state(self, news: list) -> None:
        if not hasattr(self, '_seen_titles'):
            self._seen_titles: set[str] = set()
        for item in news:
            _, title, _, _ = self._normalize_news_item(item)
            if title in self._seen_titles:
                continue
            self._seen_titles.add(title)
            if self._should_render_news_item(item):
                self._render_news_item(item)

    def refresh_data(self, engine) -> None:
        try:
            news: list = list(getattr(engine, '_news', []))
            for item in news[self._last_len :]:
                if self._should_render_news_item(item):
                    self._render_news_item(item)
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
    #mid-row, #bot-row, #log-row {
        height: 1fr;
    }
    #top-row, #mid-row, #bot-row, #log-row {
        layout: horizontal;
    }
    #left-col {
        width: 34;
        layout: vertical;
    }
    DecisionsTable, ActivityLog, OrderBooksTable, NewsLog, PositionsPanel, OrdersPanel {
        width: 1fr;
    }
    """

    # Monitor is read-only for keyboard — buttons and coinjure engine CLI control engine.
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
            yield DecisionsTable(id='decisions')
        with Horizontal(id='mid-row'):
            yield PositionsPanel(id='positions')
            yield OrdersPanel(id='orders')
        with Horizontal(id='bot-row'):
            yield OrderBooksTable(id='orderbooks')
            yield NewsLog(id='news', highlight=True, markup=True)
        with Horizontal(id='log-row'):
            yield ActivityLog(id='activity', highlight=True, markup=True)
        yield ControlBar(id='ctrl-bar')
        yield Footer()

    def on_mount(self) -> None:
        self.title = (
            f'Coinjure — {self.exchange_name}' if self.exchange_name else 'Coinjure'
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
            # Stop the engine first (data source cleanup, state save, alerter).
            # Must run even when the worker is cancelled (E-Stop / quit).
            try:
                await self.engine.stop()
            except Exception:
                self.engine.request_stop()
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
            '⏸  PAUSED — click ▶ Resume or: coinjure engine resume'
            if paused
            else '▶  Running — click ⏸ Pause or: coinjure engine pause'
        )
        self.sub_title = (
            f'{self.sub_title}  |  Last: {last_activity[:60]}  |  E-Stop: s'
        )
        try:
            self.query_one('#ctrl-bar', ControlBar).update_state(paused, connected=True)
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle control-bar button clicks (same effect as coinjure engine CLI)."""
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
                try:
                    self.query_one('#btn-stop', Button).label = '⚠  Confirm?'
                except Exception:
                    pass
                self.sub_title = '⚠  Click ⏹ E-Stop AGAIN to confirm — 3 s to cancel'
                self.set_timer(3.0, self._disarm_stop)
            else:
                self._stop_armed = False
                self.notify('⏹  Stopping engine…', severity='error', timeout=5)
                self.engine.request_stop()
                self.exit()

    def _disarm_stop(self) -> None:
        self._stop_armed = False
        try:
            self.query_one('#btn-stop', Button).label = '⏹  E-Stop'
        except Exception:
            pass

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
            self.query_one('#decisions', DecisionsTable).refresh_data(strategy)
            self.query_one('#positions', PositionsPanel).refresh_data(trader, pm)
            self.query_one('#orders', OrdersPanel).refresh_data(trader)
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

    def action_quit(self) -> None:  # type: ignore[override]
        """q — exit the app (worker and engine stop automatically)."""
        self.exit()

    def action_estop(self) -> None:
        """s — keyboard emergency stop (same two-step guard as button)."""
        if not self._stop_armed:
            self._stop_armed = True
            try:
                self.query_one('#btn-stop', Button).label = '⚠  Confirm?'
            except Exception:
                pass
            self.sub_title = (
                '⚠  Press s AGAIN to confirm emergency stop — 3 s to cancel'
            )
            self.set_timer(3.0, self._disarm_stop)
            return
        self._stop_armed = False
        self.notify('⏹  Stopping engine…', severity='error', timeout=5)
        self.engine.request_stop()
        self.exit()


# ── Standalone socket monitor (independent process) ────────────────────


class SocketTradingMonitorApp(App[None]):
    """Read-only monitor that connects to one or more running engines via Unix sockets.

    When multiple sockets are provided, decisions/positions/orders/portfolio are
    aggregated across all engines so the view shows the full portfolio at once.

    Runs in a completely separate process. Engines continue unaffected
    when this app is closed.

    Start with:  coinjure engine monitor   (auto-discovers all running engines)
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

    def __init__(
        self,
        socket_path: Path | None = None,
        socket_paths: list[Path] | None = None,
        socket_labels: dict[Path, str] | None = None,
        *,
        auto_discover: bool = False,
    ) -> None:
        from coinjure.engine.control import SOCKET_PATH

        super().__init__()
        # Accept either a single socket (legacy) or a list of sockets.
        if socket_paths:
            self._socket_paths: list[Path] = list(socket_paths)
        elif socket_path:
            self._socket_paths = [socket_path]
        else:
            self._socket_paths = [SOCKET_PATH]
        # socket_labels maps socket path → human-readable strategy label
        self._socket_labels: dict[Path, str] = socket_labels or {}
        self._auto_discover = auto_discover
        self._monitor_start = datetime.now()
        self._connected: bool = False
        self._paused: bool = False
        self._stop_armed: bool = False  # two-click confirmation for E-Stop

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id='top-row'):
            with Vertical(id='left-col'):
                yield PortfolioPanel(id='portfolio')
                yield StatsPanel(self._monitor_start, id='stats')
            yield DecisionsTable(id='decisions')
        with Horizontal(id='mid-row'):
            yield PositionsPanel(id='positions')
            yield OrdersPanel(id='orders')
        with Horizontal(id='bot-row'):
            yield OrderBooksTable(id='orderbooks')
            yield NewsLog(id='news', highlight=True, markup=True)
        with Horizontal(id='log-row'):
            yield ActivityLog(id='activity', highlight=True, markup=True)
        yield ControlBar(id='ctrl-bar')
        yield Footer()

    def on_mount(self) -> None:
        n = len(self._socket_paths)
        self.title = f'Coinjure — Monitor ({n} engine{"s" if n != 1 else ""})'
        self.sub_title = 'Waiting for engines…'
        self._strategy_count: int = 0  # updated after first poll
        self.call_after_refresh(self._set_initial_disconnected_state)
        self.set_interval(2.0, self._poll_state)

    def _set_initial_disconnected_state(self) -> None:
        try:
            self.query_one('#ctrl-bar', ControlBar).update_state(False, connected=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Multi-engine polling and aggregation
    # ------------------------------------------------------------------

    def _engine_tag(self, sock: Path) -> str:
        """Short label for an engine: strategy_id from registry, else socket stem."""
        if sock in self._socket_labels:
            return self._socket_labels[sock][:14]
        name = sock.stem.removeprefix('engine-')
        return name[:14]

    async def _fetch_one(self, sock: Path) -> dict | None:
        from coinjure.engine.control import send_command

        try:
            return await send_command('get_state', socket_path=sock)
        except Exception:
            return None

    def _rediscover_sockets(self) -> None:
        """Rescan registry and socket directory for newly started engines."""
        from coinjure.engine.control import SOCKET_DIR, cleanup_stale_sockets
        from coinjure.engine.registry import StrategyRegistry

        cleanup_stale_sockets()

        registry_path = Path.home() / '.coinjure' / 'portfolio.json'
        known = set(self._socket_paths)
        new_labels: dict[Path, str] = {}

        # Scan registry for new sockets
        if registry_path.exists():
            try:
                registry = StrategyRegistry(registry_path)
                for entry in registry.list():
                    if entry.socket_path:
                        p = Path(entry.socket_path)
                        if p.exists() and p not in known:
                            new_labels[p] = entry.strategy_id
            except Exception:
                pass

        # Scan socket directory for unregistered engines
        for sock in sorted(SOCKET_DIR.glob('engine-*.sock')):
            if sock not in known and sock not in new_labels:
                new_labels[sock] = sock.stem

        if new_labels:
            self._socket_paths.extend(new_labels.keys())
            self._socket_labels.update(new_labels)
            n = len(self._socket_paths)
            self.title = f'Coinjure — Monitor ({n} engine{"s" if n != 1 else ""})'

        # Also prune sockets that no longer exist
        self._socket_paths = [p for p in self._socket_paths if p.exists()]

    def _clear_all_panels(self) -> None:
        """Reset all panels to empty state when no engines are reachable."""
        try:
            self.query_one('#ctrl-bar', ControlBar).update_state(False, connected=False)
        except Exception:
            pass

        empty_state: dict = {
            'paused': False,
            'data_paused': False,
            'runtime': '—',
            'portfolio': {
                'total': 0.0,
                'realized_pnl': 0.0,
                'unrealized_pnl': 0.0,
                'cash_positions': [],
            },
            'positions': [],
            'orders': [],
            'order_books': [],
            'news': [],
            'activity_log': [],
            'stats': {
                'event_count': 0,
                'order_books': 0,
                'news_buffered': 0,
                'orders_total': 0,
                'orders_filled': 0,
                'decision_stats': {'decisions': 0, 'executed': 0},
            },
        }

        try:
            self.query_one('#portfolio', PortfolioPanel).refresh_from_state(empty_state)
            self.query_one('#stats', StatsPanel).refresh_from_state(empty_state)
            self.query_one('#decisions', DecisionsTable).clear()
            self.query_one('#positions', PositionsPanel).clear()
            self.query_one('#orders', OrdersPanel).clear()
            self.query_one('#orderbooks', OrderBooksTable).clear()
            # RichLog panels: clear content and reset dedup state
            activity = self.query_one('#activity', ActivityLog)
            activity.clear()
            activity._seen_keys = set()
            news = self.query_one('#news', NewsLog)
            news.clear()
            news._seen_titles = set()
        except Exception as exc:
            logger.debug('Clear panels error: %s', exc)

    async def _poll_state(self) -> None:
        """Fetch state from all engines in parallel and merge into one view."""
        import asyncio

        if self._auto_discover:
            self._rediscover_sockets()

        if not self._socket_paths:
            self._connected = False
            self.sub_title = (
                '⚠  No engines reachable — start one with: coinjure engine paper-run'
            )
            self._clear_all_panels()
            return

        results: list[dict | None] = await asyncio.gather(
            *[self._fetch_one(s) for s in self._socket_paths]
        )

        # Filter out failed sockets
        states: list[tuple[Path, dict]] = [
            (sock, state)
            for sock, state in zip(self._socket_paths, results, strict=False)
            if state is not None
        ]

        connected_count = len(states)
        if connected_count == 0:
            self._connected = False
            self.sub_title = (
                '⚠  No engines reachable — start one with: coinjure engine paper-run'
            )
            self._clear_all_panels()
            return

        self._connected = True
        any_paused = any(s.get('paused', False) for _, s in states)
        self._paused = any_paused

        # --- Merge decisions (label each with strategy id + type) ---

        merged_decisions: list[dict] = []
        multi = len(states) > 1
        for sock, state in states:
            tag = self._engine_tag(sock)
            strategy_name = state.get('strategy_name', '')
            for d in state.get('decisions', []):
                d2 = dict(d)
                # Preserve per-decision strategy_name from multi-engine aggregation;
                # only fall back to the engine-level name if missing.
                if not d2.get('strategy_name'):
                    d2['strategy_name'] = strategy_name
                if multi:
                    d2['ticker_name'] = tag[:18]
                merged_decisions.append(d2)
        # Sort by timestamp string (lexicographic works for HH:MM:SS)
        merged_decisions.sort(key=lambda d: d.get('timestamp', ''))

        # --- Merge portfolio (sum across all engines) ---
        total_sum = sum(s.get('portfolio', {}).get('total', 0.0) for _, s in states)
        realized_sum = sum(
            s.get('portfolio', {}).get('realized_pnl', 0.0) for _, s in states
        )
        unrealized_sum = sum(
            s.get('portfolio', {}).get('unrealized_pnl', 0.0) for _, s in states
        )
        # Merge cash positions across engines (sum by symbol)
        cash_by_symbol: dict[str, float] = {}
        for _, s in states:
            for cp in s.get('portfolio', {}).get('cash_positions', []):
                sym = cp.get('symbol', '')
                cash_by_symbol[sym] = cash_by_symbol.get(sym, 0.0) + cp.get('qty', 0.0)
        merged_portfolio = {
            'total': total_sum,
            'realized_pnl': realized_sum,
            'unrealized_pnl': unrealized_sum,
            'cash_positions': [
                {'symbol': k, 'qty': v} for k, v in cash_by_symbol.items()
            ],
        }

        # --- Merge positions, orders, orderbooks, news, activity ---
        merged_positions: list = []
        merged_orders: list = []
        merged_orderbooks: list = []
        merged_news: list = []
        merged_activity: list = []
        total_events = 0
        total_decisions_count = 0
        total_executed = 0
        total_ob = 0
        total_news_buf = 0
        total_orders = 0
        total_filled = 0

        for _, state in states:
            merged_positions.extend(state.get('positions', []))
            merged_orders.extend(state.get('orders', []))
            merged_orderbooks.extend(state.get('order_books', []))
            merged_news.extend(state.get('news', []))
            merged_activity.extend(state.get('activity_log', []))
            s = state.get('stats', {}) or {}
            total_events += s.get('event_count', 0) or 0
            total_ob += s.get('order_books', 0) or 0
            total_news_buf += s.get('news_buffered', 0) or 0
            total_orders += s.get('orders_total', 0) or 0
            total_filled += s.get('orders_filled', 0) or 0
            ds = s.get('decision_stats', {}) or {}
            total_decisions_count += ds.get('decisions', 0) or 0
            total_executed += ds.get('executed', 0) or 0

        # Deduplicate orderbooks by market name (same market in multiple engines)
        seen_ob: set[str] = set()
        unique_orderbooks: list = []
        for ob in merged_orderbooks:
            key = ob.get('name', '') if isinstance(ob, dict) else str(ob)
            if key not in seen_ob:
                seen_ob.add(key)
                unique_orderbooks.append(ob)

        # Build a merged state dict for panels that consume the full state
        merged_state: dict = {
            'paused': any_paused,
            'data_paused': False,
            'runtime': states[0][1].get('runtime', '—') if states else '—',
            'portfolio': merged_portfolio,
            'positions': merged_positions,
            'orders': merged_orders,
            'order_books': unique_orderbooks,
            'news': merged_news[-40:],
            'activity_log': sorted(
                merged_activity,
                key=lambda x: x[0] if isinstance(x, (list, tuple)) and x else '',
            )[-40:],
            'stats': {
                'event_count': total_events,
                'order_books': total_ob,
                'news_buffered': total_news_buf,
                'orders_total': total_orders,
                'orders_filled': total_filled,
                'decision_stats': {
                    'decisions': total_decisions_count,
                    'executed': total_executed,
                },
            },
        }

        # --- Update title & subtitle ---
        # Count strategies: multi-engine states report comma-separated strategy names
        strategy_count = 0
        for _, state in states:
            sname = state.get('strategy_name', '')
            if state.get('mode') == 'multi' and sname:
                strategy_count += len(sname.split(', '))
            elif sname:
                strategy_count += 1
            else:
                strategy_count += 1
        self._strategy_count = strategy_count

        n_engines = len(self._socket_paths)
        self.title = (
            f'Coinjure — Monitor ({strategy_count} '
            f'strateg{"ies" if strategy_count != 1 else "y"}'
            f', {n_engines} engine{"s" if n_engines != 1 else ""})'
        )
        status = '⏸  PAUSED' if any_paused else '▶  Running'
        self.sub_title = (
            f'{status}  |  {strategy_count} strategies across '
            f'{connected_count}/{n_engines} engines  |  '
            f'decisions={total_decisions_count}  executed={total_executed}  |  E-Stop: s'
        )

        try:
            self.query_one('#ctrl-bar', ControlBar).update_state(
                any_paused, connected=True
            )
        except Exception:
            pass

        try:
            self.query_one('#portfolio', PortfolioPanel).refresh_from_state(
                merged_state
            )
            self.query_one('#stats', StatsPanel).refresh_from_state(merged_state)
            self.query_one('#decisions', DecisionsTable).refresh_from_state(
                merged_decisions, multi=multi
            )
            self.query_one('#positions', PositionsPanel).refresh_from_state(
                merged_state
            )
            self.query_one('#orders', OrdersPanel).refresh_from_state(merged_state)
            self.query_one('#activity', ActivityLog).refresh_from_state(
                merged_state['activity_log']
            )
            self.query_one('#orderbooks', OrderBooksTable).refresh_from_state(
                unique_orderbooks
            )
            self.query_one('#news', NewsLog).refresh_from_state(merged_state['news'])
        except Exception as exc:
            logger.debug('Socket monitor render error: %s', exc)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle control-bar button clicks — broadcasts to all engines."""
        import asyncio

        from coinjure.engine.control import send_command

        btn_id = event.button.id

        if btn_id == 'btn-pause':
            try:
                await asyncio.gather(
                    *[send_command('pause', socket_path=s) for s in self._socket_paths],
                    return_exceptions=True,
                )
                self.notify('⏸  All engines paused', severity='warning', timeout=3)
            except Exception as exc:
                self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

        elif btn_id == 'btn-resume':
            try:
                await asyncio.gather(
                    *[
                        send_command('resume', socket_path=s)
                        for s in self._socket_paths
                    ],
                    return_exceptions=True,
                )
                self.notify('▶  All engines resumed', timeout=2)
            except Exception as exc:
                self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

        elif btn_id == 'btn-stop':
            if not self._stop_armed:
                self._stop_armed = True
                try:
                    self.query_one('#btn-stop', Button).label = '⚠  Confirm?'
                except Exception:
                    pass
                self.sub_title = '⚠  Click ⏹ E-Stop AGAIN to confirm — 3 s to cancel'
                self.set_timer(3.0, self._disarm_stop)
            else:
                self._stop_armed = False
                try:
                    self.query_one('#btn-stop', Button).label = '⏹  E-Stop'
                except Exception:
                    pass
                try:
                    await asyncio.gather(
                        *[
                            send_command('stop', socket_path=s)
                            for s in self._socket_paths
                        ],
                        return_exceptions=True,
                    )
                    self.notify(
                        '⏹  Stop signal sent to all engines',
                        severity='error',
                        timeout=4,
                    )
                except Exception as exc:
                    self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)

    def _disarm_stop(self) -> None:
        self._stop_armed = False
        try:
            self.query_one('#btn-stop', Button).label = '⏹  E-Stop'
        except Exception:
            pass

    def action_scroll_down_panel(self) -> None:
        focused = self.focused
        if focused is not None:
            focused.scroll_down()

    def action_scroll_up_panel(self) -> None:
        focused = self.focused
        if focused is not None:
            focused.scroll_up()

    async def action_e_stop(self) -> None:
        """s — keyboard emergency stop broadcast to all engines."""
        import asyncio

        from coinjure.engine.control import send_command

        if not self._stop_armed:
            self._stop_armed = True
            try:
                self.query_one('#btn-stop', Button).label = '⚠  Confirm?'
            except Exception:
                pass
            self.sub_title = (
                '⚠  Press s AGAIN to confirm emergency stop — 3 s to cancel'
            )
            self.set_timer(3.0, self._disarm_stop)
            return

        self._stop_armed = False
        try:
            self.query_one('#btn-stop', Button).label = '⏹  E-Stop'
        except Exception:
            pass
        try:
            await asyncio.gather(
                *[send_command('stop', socket_path=s) for s in self._socket_paths],
                return_exceptions=True,
            )
            self.notify(
                '⏹  Stop signal sent to all engines', severity='error', timeout=4
            )
        except Exception as exc:
            self.notify(f'⚠  Error: {exc}', severity='error', timeout=5)
