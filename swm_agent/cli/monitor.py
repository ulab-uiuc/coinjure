"""Full-screen real-time trading dashboard (TUI).

Architecture
------------
* **QueueHandler** intercepts all Python logging and stores records in two
  ``deque`` ring-buffers (system logs & strategy / news signals).
* **DemoDataSource** + **DemoStrategy** provide a self-contained simulation
  when the ``--demo`` flag is set.
* The ``monitor`` Click command starts an **asyncio** event-loop that runs
  ``TradingEngine.start()`` as a background task while the foreground task
  refreshes the Rich ``Live`` display every *1 / refresh* seconds, reading
  ``engine.get_snapshot()`` each tick.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from datetime import datetime
from decimal import Decimal

import click
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swm_agent.core.trading_engine import EngineSnapshot, TradingEngine
from swm_agent.data.data_source import DataSource
from swm_agent.data.market_data_manager import MarketDataManager
from swm_agent.events.events import Event, NewsEvent, PriceChangeEvent
from swm_agent.position.position_manager import Position, PositionManager
from swm_agent.risk.risk_manager import StandardRiskManager
from swm_agent.strategy.strategy import Strategy
from swm_agent.ticker.ticker import CashTicker, PolyMarketTicker
from swm_agent.trader.paper_trader import PaperTrader
from swm_agent.trader.trader import Trader
from swm_agent.trader.types import TradeSide

# ---------------------------------------------------------------------------
# 1. Log Interception — QueueHandler
# ---------------------------------------------------------------------------

_STRATEGY_LOGGERS = ('swm_agent.strategy',)


class TUILogHandler(logging.Handler):
    """A ``logging.Handler`` that routes records into two deques.

    * **log_deque** — general system messages (engine, risk, data, …).
    * **signal_deque** — strategy / news-signal messages (identified by
      logger-name prefix ``swm_agent.strategy``).

    Install on the *root* logger **before** starting the engine so that
    every ``logger.info(…)`` call in the project is intercepted rather
    than printed to ``stdout`` (which would corrupt the Rich display).
    """

    def __init__(
        self,
        log_deque: deque[str],
        signal_deque: deque[tuple[str, str, str]],
    ) -> None:
        super().__init__()
        self.log_deque = log_deque
        self.signal_deque = signal_deque
        # [H2] Both deques are created with ``maxlen`` by the caller, so
        # they automatically evict old entries — no manual popleft needed.

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            ts = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')

            # Route strategy messages to the signal panel
            if any(record.name.startswith(pfx) for pfx in _STRATEGY_LOGGERS):
                signal = _extract_signal(msg)
                self.signal_deque.append((ts, msg, signal))
                return

            # Colour-code by level
            if record.levelno >= logging.ERROR:
                styled = f'[red]{ts}  {msg}[/red]'
            elif record.levelno >= logging.WARNING:
                styled = f'[yellow]{ts}  {msg}[/yellow]'
            else:
                styled = f'[dim]{ts}[/dim]  {msg}'

            self.log_deque.append(styled)
        except Exception:
            self.handleError(record)


def _extract_signal(msg: str) -> str:
    """Heuristically detect BUY / SELL / HOLD in a log message."""
    low = msg.lower()
    if 'buy' in low:
        return '[green]BUY[/green]'
    if 'sell' in low:
        return '[red]SELL[/red]'
    if 'hold' in low:
        return '[yellow]HOLD[/yellow]'
    return '[dim]—[/dim]'


# ---------------------------------------------------------------------------
# 2. Demo data source + strategy
# ---------------------------------------------------------------------------

_DEMO_TICKERS = [
    PolyMarketTicker(symbol='BTC-YES', name='Bitcoin > $70k', market_id='1', event_id='1'),
    PolyMarketTicker(symbol='TRUMP-YES', name='Trump wins 2024', market_id='2', event_id='2'),
    PolyMarketTicker(symbol='FED-CUT-YES', name='Fed rate cut Q1', market_id='3', event_id='3'),
    PolyMarketTicker(symbol='ETH-YES', name='ETH ETF approved', market_id='4', event_id='4'),
]

_DEMO_HEADLINES = [
    'Fed holds rates steady at 5.25-5.50%',
    'CPI data comes in below expectations',
    'Bitcoin breaks $68k resistance level',
    'SEC delays ETH ETF decision to Q3',
    'Polymarket volume surges 40% WoW',
    'Tech earnings beat estimates across the board',
    'Unemployment claims rise unexpectedly',
    'Oil prices spike on geopolitical tensions',
    'Treasury yields fall on soft jobs data',
    'Market volatility index rises sharply',
]


class DemoDataSource(DataSource):
    """Generates a stream of random ``PriceChangeEvent`` / ``NewsEvent``."""

    def __init__(self, speed: float = 1.0) -> None:
        self._prices = {t.symbol: Decimal('0.50') for t in _DEMO_TICKERS}
        self._speed = speed

    async def get_next_event(self) -> Event | None:
        await asyncio.sleep(random.uniform(0.3, 1.2) / self._speed)

        ticker = random.choice(_DEMO_TICKERS)

        # ~15 % chance of a news event
        if random.random() < 0.15:
            return NewsEvent(
                news=random.choice(_DEMO_HEADLINES),
                title=random.choice(_DEMO_HEADLINES),
                source='DemoNews',
                ticker=ticker,
            )

        # Price drift
        delta = Decimal(str(round(random.uniform(-0.015, 0.015), 4)))
        new_price = max(
            Decimal('0.05'),
            min(Decimal('0.95'), self._prices[ticker.symbol] + delta),
        )
        self._prices[ticker.symbol] = new_price
        return PriceChangeEvent(ticker=ticker, price=new_price)


class DemoStrategy(Strategy):
    """Momentum-following strategy that uses *logging* instead of print."""

    _logger = logging.getLogger('swm_agent.strategy.demo')

    def __init__(self, qty: Decimal = Decimal('50')) -> None:
        self._last: dict[str, Decimal] = {}
        self._qty = qty

    async def process_event(self, event: Event, trader: Trader) -> None:
        if isinstance(event, NewsEvent):
            self._logger.info('News: %s', event.title or event.news[:60])
            # Simulate an LLM-style signal
            action = random.choice(['BUY', 'SELL', 'HOLD'])
            conf = round(random.uniform(0.3, 0.95), 2)
            self._logger.info(
                'LLM signal: %s  confidence=%.2f  ticker=%s',
                action, conf, event.ticker.symbol if event.ticker else '?',
            )
            return

        if not isinstance(event, PriceChangeEvent):
            return

        sym = event.ticker.symbol
        price = event.price

        if sym in self._last:
            diff = price - self._last[sym]
            if diff > Decimal('0.005'):
                self._logger.info(
                    'BUY %s @ %s (momentum +%s)', sym, price, diff,
                )
                await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=event.ticker,
                    limit_price=price + Decimal('0.01'),
                    quantity=self._qty,
                )
            elif diff < Decimal('-0.005'):
                pos = trader.position_manager.get_position(event.ticker)
                sell_qty = min(self._qty, pos.quantity) if pos and pos.quantity > 0 else Decimal('0')
                if sell_qty > 0:
                    self._logger.info(
                        'SELL %s @ %s (momentum %s)', sym, price, diff,
                    )
                    await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=event.ticker,
                        limit_price=price - Decimal('0.01'),
                        quantity=sell_qty,
                    )

        self._last[sym] = price


# ---------------------------------------------------------------------------
# 3. Layout
# ---------------------------------------------------------------------------

def make_layout() -> Layout:
    """Build the dashboard skeleton.

    ::

        ┌────────────────────────────────────────────────┐
        │  Header   (size=3)                             │
        ├───────────────────────┬────────────────────────┤
        │  Left: Market Data    │  Right: Portfolio       │
        │  ├ OrderBook          │  ├ Positions            │
        │  └ Recent Trades      │  ├ Risk Indicators      │
        │                       │  └ Active Orders        │
        ├───────────────────────┴────────────────────────┤
        │  Footer  (size=12)                             │
        │  ├ System Logs        │  News Signals (LLM)    │
        └────────────────────────────────────────────────┘
    """
    layout = Layout(name='root')
    layout.split_column(
        Layout(name='header', size=3),
        Layout(name='main', ratio=1),
        Layout(name='footer', size=12),
    )
    layout['main'].split_row(
        Layout(name='left', ratio=1),
        Layout(name='right', ratio=1),
    )
    layout['left'].split_column(
        Layout(name='orderbook', ratio=3),
        Layout(name='recent_trades', ratio=2),
    )
    layout['right'].split_column(
        Layout(name='positions', ratio=3),
        Layout(name='risk', ratio=1),
        Layout(name='active_orders', ratio=2),
    )
    layout['footer'].split_row(
        Layout(name='logs', ratio=1),
        Layout(name='news_signals', ratio=1),
    )
    return layout


# ---------------------------------------------------------------------------
# 4. Panel generators  (all driven by EngineSnapshot)
# ---------------------------------------------------------------------------

def _pnl_style(value: Decimal) -> str:
    if value > 0:
        return f'[green]+${value:.2f}[/green]'
    if value < 0:
        return f'[red]-${abs(value):.2f}[/red]'
    return f'[white]${value:.2f}[/white]'


def _pct_style(value: float) -> str:
    if value > 0:
        return f'[green]+{value:.2f}%[/green]'
    if value < 0:
        return f'[red]{value:.2f}%[/red]'
    return f'[white]{value:.2f}%[/white]'


def _dd_color(pct: float) -> str:
    if pct < 5.0:
        return 'green'
    if pct < 10.0:
        return 'yellow'
    return 'red'


# ---- Header ---------------------------------------------------------------

def generate_header(snap: EngineSnapshot) -> Panel:
    pnl_m = _pnl_style(snap.total_pnl)
    sh_val = float(snap.sharpe)
    sh_color = 'green' if sh_val >= 1.0 else ('yellow' if sh_val >= 0 else 'red')
    status = '[green]RUNNING[/green]' if snap.engine_running else '[red]STOPPED[/red]'

    parts = [
        f'[bold cyan]SWM Agent[/bold cyan] {status}  [dim]|[/dim]  ',
        f'[bold]Equity[/bold] [white]${snap.equity:,.2f}[/white]  [dim]|[/dim]  ',
        f'[bold]PnL[/bold] {pnl_m}  [dim]|[/dim]  ',
        f'[bold]Sharpe[/bold] [{sh_color}]{sh_val:.2f}[/{sh_color}]  [dim]|[/dim]  ',
        f'[bold]Events[/bold] [white]{snap.event_count}[/white]  [dim]|[/dim]  ',
        f'[bold]Uptime[/bold] [white]{snap.uptime}[/white]  [dim]|[/dim]  ',
        f'[dim]{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}[/dim]',
    ]
    return Panel(
        Align.center(Text.from_markup(''.join(parts))),
        style='bold blue',
        height=3,
    )


# ---- OrderBook -------------------------------------------------------------


def generate_orderbook(snap: EngineSnapshot) -> Panel:
    if not snap.orderbooks:
        return Panel(
            Align.center(Text('Waiting for market data…', style='dim')),
            title='[bold] Live OrderBook [/bold]',
            border_style='bright_blue',
        )

    grid = Table.grid(padding=(0, 1))
    grid.add_column(ratio=1)

    for ob in snap.orderbooks[:4]:  # show up to 4 tickers
        t = Table(
            title=f'[bold cyan]{ob.ticker_symbol}[/bold cyan]',
            show_header=True,
            header_style='bold',
            expand=True,
            title_justify='left',
            padding=(0, 1),
        )
        t.add_column('Bid Size', justify='right', style='green')
        t.add_column('Bid', justify='right', style='green')
        t.add_column('Ask', justify='right', style='red')
        t.add_column('Ask Size', justify='right', style='red')

        depth = max(len(ob.bids), len(ob.asks))
        for i in range(min(depth, 5)):
            bp = f'{ob.bids[i][0]:.4f}' if i < len(ob.bids) else ''
            bs = f'{ob.bids[i][1]:,.0f}' if i < len(ob.bids) else ''
            ap = f'{ob.asks[i][0]:.4f}' if i < len(ob.asks) else ''
            az = f'{ob.asks[i][1]:,.0f}' if i < len(ob.asks) else ''
            t.add_row(bs, bp, ap, az)

        if ob.bids and ob.asks:
            spread = ob.asks[0][0] - ob.bids[0][0]
            sp_pct = (
                float(spread / ob.bids[0][0] * 100)
                if ob.bids[0][0] > 0
                else 0.0
            )
            t.add_row(
                '', '[dim]spread[/dim]',
                f'[dim]{spread:.4f} ({sp_pct:.2f}%)[/dim]', '',
            )
        grid.add_row(t)

    return Panel(
        grid,
        title='[bold] Live OrderBook [/bold]',
        border_style='bright_blue',
    )


# ---- Recent Trades ---------------------------------------------------------


def generate_recent_trades(snap: EngineSnapshot) -> Panel:
    if not snap.recent_trades:
        return Panel(
            Align.center(Text('No trades yet', style='dim')),
            title='[bold] Recent Trades [/bold]',
            border_style='yellow',
        )
    table = Table(
        show_header=True, header_style='bold', expand=True, padding=(0, 1),
    )
    table.add_column('Time', style='dim', width=10)
    table.add_column('Side', width=5)
    table.add_column('Ticker', style='cyan')
    table.add_column('Price', justify='right')
    table.add_column('Qty', justify='right')
    table.add_column('Status', width=10)

    for tr in snap.recent_trades[:8]:
        side_m = (
            f'[green]{tr.side}[/green]'
            if tr.side == 'BUY'
            else f'[red]{tr.side}[/red]'
        )
        st_m = (
            '[green]FILLED[/green]'
            if tr.status == 'FILLED'
            else f'[yellow]{tr.status}[/yellow]'
        )
        table.add_row(
            tr.time, side_m, tr.ticker_symbol,
            f'${tr.price:.4f}', f'{tr.quantity:,.0f}', st_m,
        )

    return Panel(
        table, title='[bold] Recent Trades [/bold]', border_style='yellow',
    )


# ---- Positions -------------------------------------------------------------


def generate_positions(snap: EngineSnapshot) -> Panel:
    if not snap.positions:
        return Panel(
            Align.center(Text('No open positions', style='dim')),
            title='[bold] Positions [/bold]',
            border_style='green',
        )

    table = Table(
        show_header=True, header_style='bold', expand=True, padding=(0, 1),
    )
    table.add_column('Ticker', style='cyan')
    table.add_column('Qty', justify='right')
    table.add_column('Avg Cost', justify='right')
    table.add_column('Current', justify='right')
    table.add_column('Chg %', justify='right')
    table.add_column('Unrealized', justify='right')

    for p in snap.positions:
        chg = (
            float((p.current_price - p.average_cost) / p.average_cost * 100)
            if p.average_cost > 0
            else 0.0
        )
        table.add_row(
            p.ticker_symbol,
            f'{p.quantity:,.0f}',
            f'${p.average_cost:.4f}',
            f'${p.current_price:.4f}',
            _pct_style(chg),
            _pnl_style(p.unrealized_pnl),
        )

    # summary
    total_val = sum(
        p.current_price * p.quantity for p in snap.positions
    )
    table.add_row(
        '[bold]TOTAL[/bold]', '', '', f'[bold]${total_val:,.2f}[/bold]',
        '', _pnl_style(snap.unrealized_pnl),
    )

    return Panel(
        table, title='[bold] Positions [/bold]', border_style='green',
    )


# ---- Risk ------------------------------------------------------------------


def generate_risk(snap: EngineSnapshot) -> Panel:
    table = Table(show_header=False, box=None, expand=True, padding=(0, 2))
    table.add_column('Metric', style='bold')
    table.add_column('Value', justify='right')

    max_dd_f = float(snap.max_drawdown_pct * 100)
    cur_dd_f = float(snap.current_drawdown_pct * 100)
    c1 = _dd_color(max_dd_f)
    c2 = _dd_color(cur_dd_f)

    table.add_row('Max Drawdown', f'[{c1}]{max_dd_f:.2f}%[/{c1}]')
    table.add_row('Current Drawdown', f'[{c2}]{cur_dd_f:.2f}%[/{c2}]')

    exp_c = 'green' if snap.exposure_pct < 50 else ('yellow' if snap.exposure_pct < 80 else 'red')
    table.add_row(
        'Exposure',
        f'[{exp_c}]${snap.exposure:,.2f} ({snap.exposure_pct:.1f}%)[/{exp_c}]',
    )
    table.add_row('Cash', f'[white]${snap.cash:,.2f}[/white]')

    return Panel(
        table,
        title='[bold] Risk Indicators [/bold]',
        border_style='magenta',
    )


# ---- Active Orders ---------------------------------------------------------


def generate_active_orders(snap: EngineSnapshot) -> Panel:
    if not snap.active_orders:
        return Panel(
            Align.center(Text('No active orders', style='dim')),
            title='[bold] Active Orders [/bold]',
            border_style='bright_yellow',
        )

    table = Table(
        show_header=True, header_style='bold', expand=True, padding=(0, 1),
    )
    table.add_column('Side', width=5)
    table.add_column('Ticker', style='cyan')
    table.add_column('Limit', justify='right')
    table.add_column('Qty', justify='right')
    table.add_column('Status', width=10)

    for o in snap.active_orders:
        sm = (
            f'[green]{o.side}[/green]'
            if o.side == 'BUY'
            else f'[red]{o.side}[/red]'
        )
        table.add_row(
            sm, o.ticker_symbol,
            f'${o.limit_price:.4f}', f'{o.quantity:,.0f}',
            f'[yellow]{o.status}[/yellow]',
        )

    return Panel(
        table,
        title='[bold] Active Orders [/bold]',
        border_style='bright_yellow',
    )


# ---- System Logs -----------------------------------------------------------


def generate_logs(log_deque: deque[str]) -> Panel:
    visible = list(log_deque)[-8:]
    body = '\n'.join(visible) if visible else '[dim]Waiting for events…[/dim]'
    return Panel(
        Text.from_markup(body),
        title='[bold] System Logs [/bold]',
        border_style='bright_black',
    )


# ---- News Signals ----------------------------------------------------------


def generate_news_signals(
    snap: EngineSnapshot,
    signal_deque: deque[tuple[str, str, str]],
) -> Panel:
    """Merge engine ``news_headlines`` with strategy signal logs."""
    table = Table(
        show_header=True, header_style='bold', expand=True, padding=(0, 1),
    )
    table.add_column('Time', style='dim', width=10)
    table.add_column('Headline / Signal')
    table.add_column('Action', width=12)

    rows: list[tuple[str, str, str]] = []

    # Headlines from the engine
    for ts, headline in snap.news_headlines:
        rows.append((ts, headline, '[dim]—[/dim]'))

    # Strategy signals from the log handler
    for ts, msg, signal in signal_deque:
        # Trim the logger-name prefix if present
        short = msg.split(': ', 1)[-1] if ': ' in msg else msg
        rows.append((ts, short[:60], signal))

    if not rows:
        return Panel(
            Align.center(Text('Waiting for news…', style='dim')),
            title='[bold] News Signals (LLM) [/bold]',
            border_style='bright_cyan',
        )

    # Deduplicate & show most recent
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for r in reversed(rows):
        key = r[1]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.reverse()

    for ts, text, action in unique[-6:]:
        table.add_row(ts, text, action)

    return Panel(
        table,
        title='[bold] News Signals (LLM) [/bold]',
        border_style='bright_cyan',
    )


# ---------------------------------------------------------------------------
# 5. Render helper
# ---------------------------------------------------------------------------


def render_dashboard(
    layout: Layout,
    snap: EngineSnapshot,
    log_deque: deque[str],
    signal_deque: deque[tuple[str, str, str]],
) -> None:
    """Populate every layout slot from the current snapshot + log queues."""
    layout['header'].update(generate_header(snap))
    layout['orderbook'].update(generate_orderbook(snap))
    layout['recent_trades'].update(generate_recent_trades(snap))
    layout['positions'].update(generate_positions(snap))
    layout['risk'].update(generate_risk(snap))
    layout['active_orders'].update(generate_active_orders(snap))
    layout['logs'].update(generate_logs(log_deque))
    layout['news_signals'].update(generate_news_signals(snap, signal_deque))


# ---------------------------------------------------------------------------
# 6. Engine factories (demo / live)
# ---------------------------------------------------------------------------

# Environment variable names
_ENV_POLYMARKET_KEY = 'POLYMARKET_PRIVATE_KEY'
_ENV_POLYMARKET_FUNDER = 'POLYMARKET_FUNDER'
_ENV_POLYMARKET_SIG_TYPE = 'POLYMARKET_SIGNATURE_TYPE'


def _build_demo_engine(
    initial_capital: Decimal = Decimal('10000'),
) -> TradingEngine:
    """Spin up a self-contained engine with simulated data."""
    data_source = DemoDataSource(speed=1.5)
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=initial_capital,
            average_cost=Decimal('0'),
            realized_pnl=Decimal('0'),
        )
    )

    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal('500'),
        max_position_size=Decimal('2000'),
        max_total_exposure=Decimal('8000'),
        max_drawdown_pct=Decimal('0.15'),
        max_positions=10,
        initial_capital=initial_capital,
    )

    trader = PaperTrader(
        market_data=market_data,
        risk_manager=risk_manager,
        position_manager=position_manager,
        min_fill_rate=Decimal('0.8'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.002'),
    )

    strategy = DemoStrategy(qty=Decimal('50'))

    return TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        initial_capital=initial_capital,
    )


def _build_live_engine(
    *,
    paper: bool = True,
    initial_capital: Decimal = Decimal('10000'),
    polling_interval: float = 30.0,
) -> TradingEngine:
    """Build an engine wired to the real Polymarket CLOB API.

    Parameters
    ----------
    paper
        If ``True`` (default) orders are **simulated** via ``PaperTrader``
        on top of real market data.  If ``False``, orders are sent to
        Polymarket through ``PolymarketTrader`` — this requires
        ``POLYMARKET_PRIVATE_KEY`` in the environment.
    initial_capital
        Starting USDC balance (used for ``PaperTrader`` & risk manager).
    polling_interval
        How often (seconds) the live data source polls the Polymarket API.

    Raises
    ------
    click.ClickException
        When required environment variables are missing.
    """
    import os

    # ---- lazy imports (these pull in heavy deps like feedparser / requests) ----
    try:
        from swm_agent.data.live.live_data_source import (
            LivePolyMarketDataSource,
        )
        from swm_agent.strategy.simple_strategy import SimpleStrategy
    except ImportError as exc:
        raise click.ClickException(
            f'Missing dependency for live mode: {exc}\n'
            'Run  pip install httpx feedparser py-clob-client requests  '
            'and try again.'
        ) from exc

    # ---- data source (public API, no key required) ----
    data_source = LivePolyMarketDataSource(
        polling_interval=polling_interval,
    )

    # ---- market data / positions ----
    market_data = MarketDataManager()
    position_manager = PositionManager()

    risk_manager = StandardRiskManager(
        position_manager=position_manager,
        market_data=market_data,
        max_single_trade_size=Decimal('500'),
        max_position_size=Decimal('2000'),
        max_total_exposure=Decimal('8000'),
        max_drawdown_pct=Decimal('0.15'),
        max_positions=10,
        initial_capital=initial_capital,
    )

    # ---- trader (paper vs real) ----
    if paper:
        position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=initial_capital,
                average_cost=Decimal('0'),
                realized_pnl=Decimal('0'),
            )
        )

        trader: Trader = PaperTrader(
            market_data=market_data,
            risk_manager=risk_manager,
            position_manager=position_manager,
            min_fill_rate=Decimal('0.8'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.002'),
        )
    else:
        # --- real trading — require private key ---
        private_key = os.environ.get(_ENV_POLYMARKET_KEY)
        if not private_key:
            raise click.ClickException(
                'Real-trade mode requires a Polymarket wallet private key.\n\n'
                'Please set the environment variable before running:\n'
                f'  export {_ENV_POLYMARKET_KEY}="your_private_key_here"\n\n'
                'Alternatively, use --paper (default) to simulate trades on '
                'real market data without risking real funds.'
            )

        funder = os.environ.get(_ENV_POLYMARKET_FUNDER)
        sig_type_str = os.environ.get(_ENV_POLYMARKET_SIG_TYPE, '0')
        try:
            sig_type = int(sig_type_str)
        except ValueError:
            sig_type = 0

        try:
            from swm_agent.trader.polymarket_trader import PolymarketTrader
        except ImportError as exc:
            raise click.ClickException(
                f'Missing dependency for real trading: {exc}\n'
                'Run  pip install py-clob-client  and try again.'
            ) from exc

        trader = PolymarketTrader(
            market_data=market_data,
            risk_manager=risk_manager,
            position_manager=position_manager,
            wallet_private_key=private_key,
            signature_type=sig_type,
            funder=funder,
        )

        # Seed position manager with on-chain USDC balance
        try:
            from py_clob_client.clob_types import (
                AssetType,
                BalanceAllowanceParams,
            )

            balance_info = trader.clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            on_chain_balance = (
                Decimal(balance_info['balance']) / Decimal('1000000')
            )
            position_manager.update_position(
                Position(
                    ticker=CashTicker.POLYMARKET_USDC,
                    quantity=on_chain_balance,
                    average_cost=Decimal('0'),
                    realized_pnl=Decimal('0'),
                )
            )
            logging.getLogger(__name__).info(
                'On-chain USDC balance: %s', on_chain_balance,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                'Could not fetch on-chain balance: %s — using %s USDC',
                exc, initial_capital,
            )
            position_manager.update_position(
                Position(
                    ticker=CashTicker.POLYMARKET_USDC,
                    quantity=initial_capital,
                    average_cost=Decimal('0'),
                    realized_pnl=Decimal('0'),
                )
            )

    # ---- strategy ----
    strategy = SimpleStrategy(
        trade_size=Decimal('10'),
        confidence_threshold=0.5,
    )

    return TradingEngine(
        data_source=data_source,
        strategy=strategy,
        trader=trader,
        initial_capital=initial_capital,
        continuous=True,  # live sources return None on timeout — keep going
    )


# ---------------------------------------------------------------------------
# 7. Async main loop
# ---------------------------------------------------------------------------


async def _async_monitor(
    engine: TradingEngine,
    refresh: int,
    log_deque: deque[str],
    signal_deque: deque[tuple[str, str, str]],
) -> None:
    """Run engine in background + Rich TUI in foreground."""
    _log = logging.getLogger(__name__)
    engine_task = asyncio.create_task(engine.start())

    console = Console()
    layout = make_layout()

    # First render (blank)
    snap = engine.get_snapshot()
    render_dashboard(layout, snap, log_deque, signal_deque)

    with Live(
        layout,
        console=console,
        refresh_per_second=refresh,
        screen=True,
    ) as live:
        try:
            while True:
                await asyncio.sleep(1.0 / refresh)
                snap = engine.get_snapshot()
                render_dashboard(layout, snap, log_deque, signal_deque)
                live.update(layout)

                # If the engine finished (data source exhausted), keep
                # displaying but mark as stopped.
                if engine_task.done():
                    # [C3] Retrieve & log any exception the engine raised
                    # so it doesn't get silently swallowed.
                    try:
                        engine_task.result()
                    except Exception:
                        _log.exception('Engine task ended with an error')

                    # Show final state for a couple more seconds
                    for _ in range(refresh * 3):
                        await asyncio.sleep(1.0 / refresh)
                        snap = engine.get_snapshot()
                        render_dashboard(layout, snap, log_deque, signal_deque)
                        live.update(layout)
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # [C2] Async stop — cancels data-source background tasks too.
            await engine.stop()

    # Belt-and-suspenders: ensure the engine task is fully cancelled.
    engine.request_stop()
    if not engine_task.done():
        engine_task.cancel()
    try:
        await engine_task
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# 8. Click command
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    '--watch/--no-watch', '-w/', default=True, show_default=True,
    help='Live monitoring (default: on). Use --no-watch for a single snapshot.',
)
@click.option(
    '--refresh', '-r', default=4, type=int,
    help='Refresh rate in frames per second (default: 4).',
)
@click.option(
    '--live', 'mode', flag_value='live',
    help='Connect to real Polymarket CLOB API for live market data.',
)
@click.option(
    '--demo', 'mode', flag_value='demo', default=True,
    help='Use built-in simulated data (default).',
)
@click.option(
    '--paper/--real-trades', 'paper', default=True, show_default=True,
    help=(
        'In --live mode: --paper (default) simulates order execution on '
        'real data; --real-trades sends orders to Polymarket (requires '
        f'{_ENV_POLYMARKET_KEY}).'
    ),
)
@click.option(
    '--capital', '-c', default=10000, type=float,
    help='Initial capital in USDC (default: 10000).',
)
def monitor(
    watch: bool,
    refresh: int,
    mode: str,
    paper: bool,
    capital: float,
) -> None:
    """Launch the full-screen real-time trading dashboard.

    \b
    Modes
    -----
      --demo  (default)  Simulated tickers & random news events.
      --live             Real Polymarket data via CLOB API.

    \b
    In live mode you can further choose the execution backend:
      --paper        Simulate trades on real data (safe, no funds at risk).
      --real-trades  Send real FOK orders to Polymarket
                     (requires POLYMARKET_PRIVATE_KEY).

    \b
    Examples
    --------
      swm-agent monitor                   # demo dashboard
      swm-agent monitor --live            # real data, paper trading
      swm-agent monitor --live --real-trades  # real data, real orders
      swm-agent monitor --no-watch        # single snapshot then exit
      swm-agent monitor -r 2              # 2 fps refresh rate
    """
    initial_capital = Decimal(str(capital))

    # ----- log interception -----
    log_deque: deque[str] = deque(maxlen=200)
    signal_deque: deque[tuple[str, str, str]] = deque(maxlen=100)
    handler = TUILogHandler(log_deque, signal_deque)
    handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Remove existing handlers to prevent stdout noise during TUI
    _prev_handlers = root_logger.handlers[:]
    root_logger.handlers = [handler]

    # Suppress overly-chatty third-party loggers
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('feedparser').setLevel(logging.WARNING)

    console = Console()

    try:
        # ----- build engine -----
        if mode == 'live':
            console.print(
                '[bold cyan]SWM Agent[/bold cyan] — '
                + (
                    '[yellow]LIVE (paper trading)[/yellow]'
                    if paper
                    else '[red bold]LIVE (REAL TRADES)[/red bold]'
                ),
            )
            engine = _build_live_engine(
                paper=paper,
                initial_capital=initial_capital,
            )
        else:
            engine = _build_demo_engine(initial_capital=initial_capital)

        if not watch:
            # Single snapshot — grab state, print, exit
            console.print(
                '[bold cyan]SWM Agent Monitor[/bold cyan] — '
                'single snapshot (engine not started)\n'
            )
            snap = engine.get_snapshot()
            layout = make_layout()
            render_dashboard(layout, snap, log_deque, signal_deque)
            console.print(layout)
            return

        # ----- live dashboard -----
        try:
            asyncio.run(
                _async_monitor(engine, refresh, log_deque, signal_deque),
            )
        except KeyboardInterrupt:
            pass

    finally:
        # Restore original logging handlers
        root_logger.handlers = _prev_handlers

    console.print(
        '\n[bold cyan]SWM Agent Monitor[/bold cyan] stopped. Goodbye!',
    )
