"""Token trading CLI commands for Coinjure."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation

import click
from rich.console import Console
from rich.table import Table

from coinjure.data.market_data_manager import MarketDataManager
from coinjure.order.order_book import OrderBook
from coinjure.position.position_manager import Position, PositionManager
from coinjure.risk.risk_manager import NoRiskManager
from coinjure.ticker.ticker import CashTicker, PolyMarketTicker
from coinjure.trader.paper_trader import PaperTrader
from coinjure.trader.types import PlaceOrderResult, TradeSide

console = Console()


# -- Adapter protocol & paper implementation --------------------------------


class PaperTokenAdapter:
    """Paper-mode adapter wrapping PaperTrader for token-level CLI use."""

    def __init__(self, initial_capital: Decimal = Decimal('10000')) -> None:
        self.market_data = MarketDataManager()
        self.position_manager = PositionManager()
        self.risk_manager = NoRiskManager()
        self.trader = PaperTrader(
            market_data=self.market_data,
            risk_manager=self.risk_manager,
            position_manager=self.position_manager,
            min_fill_rate=Decimal('0.5'),
            max_fill_rate=Decimal('1.0'),
            commission_rate=Decimal('0.0'),
        )
        # Seed initial USDC balance.
        self.position_manager.update_position(
            Position(
                ticker=CashTicker.POLYMARKET_USDC,
                quantity=initial_capital,
                average_cost=Decimal('1'),
                realized_pnl=Decimal('0'),
            )
        )

    def _ensure_ticker(self, token_id: str) -> PolyMarketTicker:
        return PolyMarketTicker.from_token_id(token_id)

    def get_orderbook(self, token_id: str) -> OrderBook:
        ticker = self._ensure_ticker(token_id)
        if ticker in self.market_data.order_books:
            return self.market_data.order_books[ticker]
        return OrderBook()

    def get_positions(self, token_id: str | None = None) -> list[Position]:
        if token_id is not None:
            ticker = self._ensure_ticker(token_id)
            pos = self.position_manager.get_position(ticker)
            return [pos] if pos and pos.quantity != 0 else []
        return [p for p in self.position_manager.positions.values() if p.quantity != 0]

    async def place_order(
        self,
        token_id: str,
        side: TradeSide,
        price: Decimal,
        size: Decimal,
    ) -> str:
        """Place an order, return human-readable result string."""
        result = await self.place_order_result(token_id, side, price, size)
        return self._format_result(result, price=price, size=size)

    async def place_order_result(
        self,
        token_id: str,
        side: TradeSide,
        price: Decimal,
        size: Decimal,
    ) -> PlaceOrderResult:
        """Place an order and return the structured result."""
        ticker = self._ensure_ticker(token_id)
        return await self.trader.place_order(side, ticker, price, size)

    @staticmethod
    def _format_result(
        result: PlaceOrderResult, *, price: Decimal, size: Decimal
    ) -> str:
        if result.order is None:
            reason = result.failure_reason.value if result.failure_reason else 'unknown'
            return f'Order REJECTED: {reason}'
        o = result.order
        if o.filled_quantity == 0:
            return (
                f'Order PLACED (not filled — no matching liquidity)\n'
                f'  Side: {o.side.value.upper()}\n'
                f'  Price: ${price}\n'
                f'  Size: {size}'
            )
        return (
            f'Order {o.status.value.upper()}\n'
            f'  Side: {o.side.value.upper()}\n'
            f'  Filled: {o.filled_quantity} @ ${o.average_price}\n'
            f'  Remaining: {o.remaining}\n'
            f'  Commission: ${o.commission}'
        )


# -- Click command group ----------------------------------------------------

_adapter: PaperTokenAdapter | None = None


def _get_adapter() -> PaperTokenAdapter:
    global _adapter  # noqa: PLW0603
    if _adapter is None:
        _adapter = PaperTokenAdapter()
    return _adapter


@click.group()
def token() -> None:
    """Token-level trading commands (paper mode)."""


@token.command()
@click.argument('token_id')
def orderbook(token_id: str) -> None:
    """Show the order book for a token."""
    adapter = _get_adapter()
    ob = adapter.get_orderbook(token_id)

    table = Table(title=f'Order Book: {token_id[:16]}...')
    table.add_column('Bid Size', justify='right')
    table.add_column('Bid', justify='right', style='green')
    table.add_column('Ask', justify='right', style='red')
    table.add_column('Ask Size', justify='right')

    bids = ob.get_bids()
    asks = ob.get_asks()
    depth = max(len(bids), len(asks))

    if depth == 0:
        console.print('[dim]No order book data available.[/dim]')
        return

    for i in range(depth):
        bid_px = f'${bids[i].price:.4f}' if i < len(bids) else ''
        bid_sz = str(bids[i].size) if i < len(bids) else ''
        ask_px = f'${asks[i].price:.4f}' if i < len(asks) else ''
        ask_sz = str(asks[i].size) if i < len(asks) else ''
        table.add_row(bid_sz, bid_px, ask_px, ask_sz)

    console.print(table)


@token.command()
@click.option('--token', 'token_id', default=None, help='Filter by token ID.')
def positions(token_id: str | None) -> None:
    """Show current positions."""
    adapter = _get_adapter()
    pos_list = adapter.get_positions(token_id)

    if not pos_list:
        console.print('[dim]No open positions.[/dim]')
        return

    table = Table(title='Positions')
    table.add_column('Symbol')
    table.add_column('Quantity', justify='right')
    table.add_column('Avg Cost', justify='right')
    table.add_column('Realized PnL', justify='right')

    for p in pos_list:
        table.add_row(
            p.ticker.symbol[:24],
            str(p.quantity),
            f'${p.average_cost:.4f}',
            f'${p.realized_pnl:.4f}',
        )

    console.print(table)


@token.command()
@click.option('--token', 'token_id', required=True, help='Token ID.')
@click.option(
    '--side', required=True, type=click.Choice(['buy', 'sell'], case_sensitive=False)
)
@click.option('--price', required=True, type=str, help='Limit price.')
@click.option('--size', required=True, type=str, help='Quantity.')
@click.option(
    '--json',
    'as_json',
    is_flag=True,
    default=False,
    help='Emit stable JSON payload.',
)
def place(token_id: str, side: str, price: str, size: str, as_json: bool) -> None:
    """Place a paper order."""
    try:
        price_d = Decimal(price)
        size_d = Decimal(size)
    except InvalidOperation:
        console.print('[red]Invalid price or size.[/red]')
        return

    trade_side = TradeSide.BUY if side.lower() == 'buy' else TradeSide.SELL
    adapter = _get_adapter()
    result = asyncio.run(
        adapter.place_order_result(token_id, trade_side, price_d, size_d)
    )
    if as_json:
        console.print(json.dumps(result.to_payload()))
        return
    human = adapter._format_result(result, price=price_d, size=size_d)
    console.print(human)
