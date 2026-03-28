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
    def best_bid_price(self) -> Decimal | None:
        """Highest bid price, or ``None`` if there are no bids."""
        best = self.best_bid
        return best.price if best is not None else None

    @property
    def best_ask_price(self) -> Decimal | None:
        """Lowest ask price, or ``None`` if there are no asks."""
        best = self.best_ask
        return best.price if best is not None else None

    @property
    def spread(self) -> Decimal | None:
        """Difference between best ask and best bid, or ``None``."""
        ask = self.best_ask_price
        bid = self.best_bid_price
        if ask is not None and bid is not None:
            return ask - bid
        return None

    @property
    def mid_price(self) -> Decimal | None:
        """Midpoint between best bid and best ask, or ``None``."""
        ask = self.best_ask_price
        bid = self.best_bid_price
        if ask is not None and bid is not None:
            return (bid + ask) / 2
        return None

    def depth(self, side: str, levels: int = 5) -> Decimal:
        """Sum of sizes for the top *levels* on *side* ('bid' or 'ask')."""
        book = self.bids if side == 'bid' else self.asks
        return sum((lvl.size for lvl in book[:levels]), Decimal(0))

    def validate(self) -> bool:
        """Check order book invariants: sorted, no negative prices/sizes."""
        for level in self.bids:
            if level.price < 0 or level.size < 0:
                return False
        for level in self.asks:
            if level.price < 0 or level.size < 0:
                return False
        for i in range(len(self.bids) - 1):
            if self.bids[i].price < self.bids[i + 1].price:
                return False
        for i in range(len(self.asks) - 1):
            if self.asks[i].price > self.asks[i + 1].price:
                return False
        return True

    def cumulative_size(self, depth_levels: int = 5) -> tuple[Decimal, Decimal]:
        """Return total size for top N bid and ask levels."""
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
