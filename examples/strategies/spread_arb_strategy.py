"""SpreadArbStrategy — intra-market mean-reversion on real orderbook data.

Buys when price dips below rolling mean, sells when it rises above.
Tracks each ticker independently so YES/NO tokens don't interfere.

Usage:
    coinjure backtest run \
        --parquet data/polymarket_orderbook_2026-03-05T06.parquet \
        --market-id 0x2c01a87e9e0fe2fb791bd87e814029efb14e6861da6d695bd5670ea6385d3d74 \
        --strategy-ref examples/strategies/spread_arb_strategy.py:SpreadArbStrategy
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from coinjure.engine.execution.trader import Trader
from coinjure.engine.execution.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import Ticker


class SpreadArbStrategy(Strategy):
    """Mean-reversion spread capture, per-ticker tracking."""

    name = 'spread_arb'
    version = '1.1.0'
    author = 'coinjure'

    def __init__(
        self,
        trade_size: float = 20.0,
        lookback: int = 15,
        entry_threshold: float = 0.005,
        max_position: float = 100.0,
    ) -> None:
        super().__init__()
        self.trade_size = Decimal(str(trade_size))
        self.lookback = lookback
        self.entry_threshold = Decimal(str(entry_threshold))
        self.max_position = Decimal(str(max_position))
        self._histories: dict[Ticker, list[Decimal]] = defaultdict(list)
        self._counts: dict[Ticker, int] = defaultdict(int)

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        mid = event.price
        history = self._histories[ticker]
        history.append(mid)
        self._counts[ticker] += 1

        if len(history) < self.lookback:
            return
        if self._counts[ticker] % 2 != 0:
            return

        window = history[-self.lookback :]
        mean = sum(window) / len(window)
        dev = mid - mean

        best_ask = trader.market_data.get_best_ask(ticker)
        best_bid = trader.market_data.get_best_bid(ticker)
        pos = trader.position_manager.get_position(ticker)
        held = pos.quantity if pos is not None else Decimal('0')

        # Buy signal: price dipped below mean
        if (
            dev < -self.entry_threshold
            and best_ask is not None
            and held < self.max_position
        ):
            qty = min(self.trade_size, self.max_position - held, best_ask.size)
            if qty > 0:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=best_ask.price,
                    quantity=qty,
                )
                self.record_decision(
                    ticker_name=ticker.symbol[:30],
                    action='BUY',
                    executed=not result.failure_reason,
                    reasoning=f'mid={mid:.4f} mean={mean:.4f} dev={dev:.4f} ask={best_ask.price}',
                    signal_values={
                        'mid': float(mid),
                        'mean': float(mean),
                        'dev': float(dev),
                    },
                )
                return

        # Sell signal: price rose above mean and we hold position
        if dev > self.entry_threshold and best_bid is not None and held > 0:
            qty = min(self.trade_size, held, best_bid.size)
            if qty > 0:
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=ticker,
                    limit_price=best_bid.price,
                    quantity=qty,
                )
                self.record_decision(
                    ticker_name=ticker.symbol[:30],
                    action='SELL',
                    executed=not result.failure_reason,
                    reasoning=f'mid={mid:.4f} mean={mean:.4f} dev={dev:.4f} bid={best_bid.price}',
                    signal_values={
                        'mid': float(mid),
                        'mean': float(mean),
                        'dev': float(dev),
                    },
                )
                return
