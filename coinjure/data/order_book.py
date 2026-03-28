from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Level:
    price: Decimal
    size: Decimal

    def __str__(self) -> str:
        return f'{self.price}@{self.size}'

    def __repr__(self) -> str:
        return f"Level(price=Decimal('{self.price}'), size=Decimal('{self.size}'))"


class OrderBook:
    def __init__(self) -> None:
        self.asks: list[Level] = []
        self.bids: list[Level] = []

    def update(self, asks: list[Level], bids: list[Level]) -> None:
        self.asks = asks
        self.bids = bids

    def __str__(self) -> str:
        best_bid = self.best_bid
        best_ask = self.best_ask
        return f'OrderBook(best_bid={best_bid}, best_ask={best_ask})'

    def __repr__(self) -> str:
        return f'OrderBook(asks={self.asks}, bids={self.bids})'

    @property
    def best_ask(self) -> Level | None:
        """Get the best ask"""
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Level | None:
        """Get the best bid"""
        return self.bids[0] if self.bids else None

    @property
    def spread(self) -> Decimal | None:
        """Return the spread (best_ask.price - best_bid.price), or None if either side is empty."""
        if self.best_ask is None or self.best_bid is None:
            return None
        return self.best_ask.price - self.best_bid.price

    def validate(self) -> bool:
        """Check order book invariants.

        Returns True if:
        - All bid prices are sorted descending
        - All ask prices are sorted ascending
        - No negative prices or sizes
        """
        # Check no negative prices or sizes
        for level in self.bids:
            if level.price < 0 or level.size < 0:
                return False
        for level in self.asks:
            if level.price < 0 or level.size < 0:
                return False
        # Check bids sorted descending
        for i in range(len(self.bids) - 1):
            if self.bids[i].price < self.bids[i + 1].price:
                return False
        # Check asks sorted ascending
        for i in range(len(self.asks) - 1):
            if self.asks[i].price > self.asks[i + 1].price:
                return False
        return True

    def cumulative_size(self, depth_levels: int = 5) -> tuple[Decimal, Decimal]:
        """Return total size for top N bid and ask levels.

        Returns a tuple of (bid_size, ask_size).
        """
        bid_size = sum(
            (level.size for level in self.bids[:depth_levels]),
            Decimal('0'),
        )
        ask_size = sum(
            (level.size for level in self.asks[:depth_levels]),
            Decimal('0'),
        )
        return bid_size, ask_size

    def get_asks(self, depth: int | None = None) -> list[Level]:
        """Get asks"""
        return self.asks[:depth] if depth else self.asks

    def get_bids(self, depth: int | None = None) -> list[Level]:
        """Get bids"""
        return self.bids[:depth] if depth else self.bids
