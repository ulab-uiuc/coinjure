from .ticker import Ticker


class PolymarketTicker(Ticker):
    def __init__(self) -> None:
        pass

    @property
    def symbol(self) -> str:
        return 'TEST'
