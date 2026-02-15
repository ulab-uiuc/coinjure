from swm_agent.data.data_source import DataSource
from swm_agent.events.events import OrderBookEvent, PriceChangeEvent
from swm_agent.strategy.strategy import Strategy
from swm_agent.trader.trader import Trader


class TradingEngine:
    def __init__(
        self, data_source: DataSource, strategy: Strategy, trader: Trader
    ) -> None:
        self.data_source = data_source
        self.strategy = strategy
        self.trader = trader
        self.market_data = trader.market_data
        self.running = False

    async def start(self) -> None:
        self.running = True
        while self.running:
            # time.sleep(1)
            event = await self.data_source.get_next_event()
            if event is None:
                self.running = False
            else:
                if isinstance(event, OrderBookEvent):
                    self.market_data.process_orderbook_event(event)
                elif isinstance(event, PriceChangeEvent):
                    self.market_data.process_price_change_event(event)
                await self.strategy.process_event(event, self.trader)

    def stop(self) -> None:
        self.running = False
