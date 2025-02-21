from data.data_source import DataSource
from events.events import OrderBookEvent
from strategy.strategy import Strategy
from trader.trader import Trader


class TradingEngine:
    def __init__(self,
        data_source: DataSource,
        strategy: Strategy,
        trader: Trader
    ) -> None:
        self.data_source = data_source
        self.strategy = strategy
        self.trader = trader
        self.market_data = trader.market_data
        self.running = False

    async def start(self):
        self.running = True
        while self.running:
            event = await self.data_source.get_next_event()
            if event is None:
                self.running = False
            else:
                if isinstance(event, OrderBookEvent):
                    self.market_data.process_orderbook_event(event)
                await self.strategy.process_event(event, self.trader)

    def stop(self):
        self.running = False
