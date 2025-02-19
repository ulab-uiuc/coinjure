from ticker.ticker import Ticker

class PolymarketTicker(Ticker):
    def __init__(self):
        pass

    @property
    def symbol(self) -> str:
        return "TEST"
