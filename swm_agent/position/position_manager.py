from dataclasses import dataclass
from decimal import Decimal

from ticker.ticker import Ticker

from data.market_data_manager import MarketDataManager


@dataclass
class Position:
    ticker: Ticker
    quantity: Decimal
    average_entry: Decimal
    realized_pnl: Decimal


class PositionManager:
    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}

    def update_position(
        self, ticker: Ticker, quantity: Decimal, price: Decimal
    ) -> Position:
        """Update position
        Args:
            ticker: The ticker
            quantity: Positive for buys, negative for sells
            price: The average execution price
        """
        if ticker.symbol not in self.positions:
            self.positions[ticker.symbol] = Position(
                ticker,
                quantity=Decimal('0'),
                average_entry=Decimal('0'),
                realized_pnl=Decimal('0'),
            )

        pos = self.positions[ticker.symbol]
        current_quantity = pos.quantity
        new_quantity = current_quantity + quantity

        if quantity < 0:
            # Calculate realized PnL for selling
            sell_quantity = abs(quantity)
            if sell_quantity > current_quantity:
                raise ValueError(
                    f'Cannot sell {sell_quantity} {ticker}, exceeding the current position {current_quantity}'
                )

            realized_pnl = (price - pos.average_entry) * sell_quantity
            pos.realized_pnl += realized_pnl

        if quantity > 0:
            # Update average entry price
            total_cost = (current_quantity * pos.average_entry) + (quantity * price)
            pos.average_entry = total_cost / new_quantity

        pos.quantity = new_quantity
        return pos

    def get_position(self, ticker: Ticker) -> Position | None:
        """Get current position for a ticker"""
        return self.positions.get(ticker.symbol)

    def get_unrealized_pnl(
        self, ticker: Ticker, market_data: MarketDataManager
    ) -> Decimal:
        """Calculate unrealized PnL for a ticker"""
        if ticker.symbol not in self.positions:
            return Decimal('0')
        raise NotImplementedError('get_unrealized_pnl not implemented')

    def get_realized_pnl(self, ticker: Ticker) -> Decimal:
        """Get realized PnL for a ticker"""
        if ticker.symbol not in self.positions:
            return Decimal('0')
        return self.positions[ticker.symbol].realized_pnl

    def get_pnl(self, ticker: Ticker, market_data: MarketDataManager) -> Decimal:
        """Get PnL for a ticker"""
        return self.get_realized_pnl(ticker) + self.get_unrealized_pnl(
            ticker, market_data
        )

    def get_total_realized_pnl(self) -> Decimal:
        """Get total realized PnL across all positions"""
        return sum(
            (pos.realized_pnl for pos in self.positions.values()), Decimal('0.0')
        )

    def get_total_unrealized_pnl(self, market_data: MarketDataManager) -> Decimal:
        """Get total unrealized PnL across all positions"""
        return sum(
            (
                self.calculate_unrealized_pnl(pos.ticker, market_data)
                for pos in self.positions.values()
            ),
            Decimal('0.0'),
        )

    def get_total_pnl(self, market_data: MarketDataManager) -> Decimal:
        """Get total PnL across all positions"""
        return self.get_total_realized_pnl() + self.get_total_unrealized_pnl(
            market_data
        )

    def calculate_unrealized_pnl(
        self, ticker: Ticker, market_data: MarketDataManager
    ) -> Decimal:
        """Calculate unrealized PnL for a ticker"""
        raise NotImplementedError('calculate_unrealized_pnl not implemented')
