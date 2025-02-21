from dataclasses import dataclass
from decimal import Decimal
from typing import List


@dataclass
class Level:
    price: Decimal
    size: Decimal

class OrderBook:
    def __init__(self):
        self.asks: List[Level] = []
        self.bids: List[Level] = []

    def update(self, asks: List[Level], bids: List[Level]):
        pass

    @property
    def best_ask(self) -> Level | None:
        """Get the best ask"""
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Level | None:
        """Get the best bid"""
        return self.bids[0] if self.bids else None

    def get_asks(self, depth: int | None = None) -> List[Level]:
        """Get asks"""
        return self.asks[:depth] if depth else self.asks

    def get_bids(self, depth: int | None = None) -> List[Level]:
        """Get bids"""
        return self.bids[:depth] if depth else self.bids
