from decimal import Decimal

from coinjure.events import Event, PriceChangeEvent
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide

from .quant_strategy import QuantStrategy


class TestStrategy(QuantStrategy):
    name = 'test'
    version = '0.1.0'
    author = 'coinjure'

    def __init__(self):
        super().__init__()
        self.last_prices = {}
        self.fixed_quantity = Decimal('100')

    async def process_event(self, event: Event, trader: Trader) -> None:
        if isinstance(event, PriceChangeEvent):
            ticker = event.ticker
            current_price = event.price

            if ticker in self.last_prices:
                last_price = self.last_prices[ticker]
                best_ask = trader.market_data.get_best_ask(ticker)
                best_bid = trader.market_data.get_best_bid(ticker)

                # Buy if price went up — use best_ask to actually cross the spread
                if current_price > last_price and best_ask is not None:
                    print(
                        f'{ticker} price increased from {last_price} to {current_price}. Buying {self.fixed_quantity} @ ask {best_ask.price}.'
                    )
                    print(
                        await trader.place_order(
                            side=TradeSide.BUY,
                            ticker=ticker,
                            limit_price=best_ask.price,
                            quantity=self.fixed_quantity,
                        )
                    )

                # Sell if price went down — use best_bid to actually hit the bid
                elif current_price < last_price and best_bid is not None:
                    print(
                        f'{ticker} price decreased from {last_price} to {current_price}. Selling {self.fixed_quantity} @ bid {best_bid.price}.'
                    )
                    print(
                        await trader.place_order(
                            side=TradeSide.SELL,
                            ticker=ticker,
                            limit_price=best_bid.price,
                            quantity=self.fixed_quantity,
                        )
                    )

            self.last_prices[ticker] = current_price

        print(event)

        cash_positions = trader.position_manager.get_cash_positions()
        print(f'Current cash: {cash_positions}')

        positions = trader.position_manager.get_non_cash_positions()
        print(f'Current positions: {positions}')

        print(trader.market_data.order_books)
