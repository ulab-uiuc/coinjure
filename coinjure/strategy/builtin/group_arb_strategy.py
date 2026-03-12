"""GroupArbStrategy — unified group constraint arbitrage.

For group relations (exclusivity, complementary) where N markets in an event
should satisfy sum(prices) ≈ 1.0 (or ≤ 1.0), this strategy:

    Case A — underpriced (sum < 1.0):
        Buy YES on every outcome.
        Cost   = sum(ask_YES_i)            < 1.0
        Payout = 1.0 (exactly one wins)
        Profit = 1.0 - sum(ask_YES_i)      > 0

    Case B — overpriced (sum > 1.0):
        Buy NO on every outcome.
        Cost   = sum(1 - ask_YES_i) = N - sum(ask_YES_i)
        Payout = N - 1 (all N-1 losing YES settle NO)
        Profit = sum(ask_YES_i) - 1.0      > 0

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/group_arb_strategy.py:GroupArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "m1-m2-m3"}'
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from coinjure.events import Event
from coinjure.market.relations import RelationStore
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import KalshiTicker, PolyMarketTicker, Ticker
from coinjure.trading.sizing import compute_trade_size
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide

logger = logging.getLogger(__name__)

_FEE_PER_SIDE = Decimal('0.005')


class GroupArbStrategy(Strategy):
    """Unified group constraint arbitrage for exclusivity/complementary relations.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be exclusivity or complementary.
    event_id:
        Polymarket event ID (alternative to relation_id for ad-hoc usage).
    min_edge:
        Minimum net profit per share after fees to trigger (default 0.02).
    trade_size:
        Dollar amount per leg.
    cooldown_seconds:
        Minimum seconds between arb executions (default 120).
    min_markets:
        Require at least this many markets before attempting arb (default 2).
    """

    name = 'group_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        event_id: str = '',
        min_edge: float = 0.02,
        close_edge: float = 0.005,
        max_loss: float = 0.05,
        trade_size: float = 100.0,
        kelly_fraction: float = 0.1,
        cooldown_seconds: int = 120,
        warmup_seconds: float = 5.0,
        min_markets: int = 2,
        llm_trade_sizing: bool = False,
    ) -> None:
        super().__init__(warmup_seconds=warmup_seconds)
        self.relation_id = relation_id
        self.min_edge = Decimal(str(min_edge))
        self.close_edge = Decimal(str(close_edge))  # close when edge drops below this
        self.max_loss = Decimal(str(max_loss))  # close when edge reverses beyond this
        self.max_trade_size = Decimal(str(trade_size))
        self.kelly_fraction = Decimal(str(kelly_fraction))
        self.cooldown_seconds = cooldown_seconds
        self._min_markets_override = min_markets
        self.llm_trade_sizing = llm_trade_sizing

        self._event_id = event_id
        self._relation_market_ids: set[str] = set()
        self._relation_token_ids: list[str] = []
        # Map token_id → market_id for tickers received via watch_token
        # (they may have empty market_id/event_id fields)
        self._token_to_market: dict[str, str] = {}
        self._no_token_ids: list[str] = []
        # Pre-built tickers with correct market_id and side for data source registration
        self._yes_tickers: dict[str, PolyMarketTicker] = {}  # yes_token_id → ticker
        self._no_tickers: dict[str, PolyMarketTicker] = {}  # no_token_id → ticker
        self._spread_type: str = ''  # 'exclusivity' or 'complementary'
        if relation_id:
            store = RelationStore()
            rel = store.get(relation_id)
            if rel:
                self._spread_type = rel.spread_type
                for m in rel.markets:
                    eid = m.get('event_id', '') or m.get('event_ticker', '')
                    if eid and not self._event_id:
                        self._event_id = eid
                    mid = (
                        m.get('id', '')
                        or m.get('market_ticker', '')
                        or m.get('ticker', '')
                    )
                    if mid:
                        self._relation_market_ids.add(mid)
                    token_ids = m.get('token_ids', [])
                    if token_ids:
                        yes_tid = token_ids[0]
                        self._relation_token_ids.append(yes_tid)
                        if mid:
                            self._token_to_market[yes_tid] = mid
                        # YES ticker with full market_id
                        self._yes_tickers[yes_tid] = PolyMarketTicker(
                            symbol=yes_tid,
                            name=m.get('question', ''),
                            token_id=yes_tid,
                            market_id=mid,
                            event_id=eid,
                            side='yes',
                        )
                        # NO token (for BUY_NO legs)
                        if len(token_ids) >= 2:
                            no_tid = token_ids[1]
                            self._no_token_ids.append(no_tid)
                            no_ticker = PolyMarketTicker(
                                symbol=no_tid,
                                name=m.get('question', ''),
                                token_id=no_tid,
                                market_id=mid,
                                event_id=eid,
                                side='no',
                            )
                            self._no_tickers[no_tid] = no_ticker

        # market_id → NO ticker for direct lookup in _check_arb
        self._market_no_ticker: dict[str, PolyMarketTicker] = {}
        for no_tid, no_t in self._no_tickers.items():
            if no_t.market_id:
                self._market_no_ticker[no_t.market_id] = no_t

        self._tickers: dict[str, Ticker] = {}
        self._last_arb_time: float = 0.0
        # Position tracking: 'BUY_YES', 'BUY_NO', or None
        self._held_direction: str | None = None

        # Require ALL markets in the relation to have prices before arbing.
        # With partial data, sum_yes is artificially low → false BUY_YES signals.
        if self._relation_market_ids:
            self.min_markets = len(self._relation_market_ids)
        else:
            self.min_markets = self._min_markets_override

    def watch_tokens(self) -> list[str]:
        tokens = list(self._relation_token_ids) + list(self._no_token_ids)
        for ticker in self._tickers.values():
            tid = getattr(ticker, 'token_id', '')
            if tid and tid not in tokens:
                tokens.append(tid)
        return tokens

    def _should_track(self, ticker: Ticker) -> bool:
        eid = ticker.event_id if hasattr(ticker, 'event_id') else ''
        mid = ticker.identifier
        if self._event_id and eid == self._event_id:
            return True
        if self._relation_market_ids and mid in self._relation_market_ids:
            return True
        # Match by token_id for tickers from watch_token (may lack market_id/event_id)
        tid = getattr(ticker, 'token_id', '')
        if tid and tid in self._token_to_market:
            return True
        return False

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        ticker = getattr(event, 'ticker', None)
        if not isinstance(ticker, (PolyMarketTicker, KalshiTicker)):
            return
        # Only track YES-side prices for sum calculation; NO-side events
        # still flow through the engine (registered in DataManager for
        # find_complement) but must not corrupt self._asks.
        if ticker.side != 'yes':
            return
        if not self._should_track(ticker):
            return

        mid = ticker.identifier
        if not mid:
            # Resolve via token_id mapping (for tickers from watch_token)
            tid = getattr(ticker, 'token_id', '')
            mid = self._token_to_market.get(tid, '')
        if not mid:
            return

        if mid not in self._tickers:
            self._tickers[mid] = ticker
            logger.debug(
                'GroupArb: registered market %s (%s)',
                mid[:16],
                ticker.name[:40] if ticker.name else '?',
            )

        await self._check_arb(trader)

    async def _check_arb(self, trader: Trader) -> None:
        if len(self._tickers) < self.min_markets:
            return

        # Query actual best asks from DataManager order book.
        # Only consider markets that belong to the relation to avoid
        # partial-data arbs when event_id matching adds extra markets.
        prices: dict[str, Decimal] = {}
        for mid, ticker in self._tickers.items():
            if self._relation_market_ids and mid not in self._relation_market_ids:
                continue
            best_ask = trader.market_data.get_best_ask(ticker)
            if best_ask is not None and best_ask.price > 0:
                prices[mid] = best_ask.price

        if len(prices) < self.min_markets:
            return
        sum_yes = sum(prices.values())
        n = len(prices)

        if logger.isEnabledFor(logging.DEBUG):
            for mid, p in prices.items():
                logger.debug(
                    '  _check_arb price: market=%s ask=%.4f', mid[:16], float(p)
                )

        edge_buy_yes = Decimal('1') - sum_yes - _FEE_PER_SIDE * n
        edge_buy_no = sum_yes - Decimal('1') - _FEE_PER_SIDE * n

        # For exclusivity relations (at most one outcome wins), BUY_YES is
        # NOT safe because if no outcome wins, all YES positions lose.
        # Only BUY_NO is guaranteed profitable when sum > 1.
        if self._spread_type == 'exclusivity':
            edge_buy_yes = Decimal('-1')  # disable BUY_YES for exclusivity

        best_edge = max(edge_buy_yes, edge_buy_no)
        preferred_action = 'BUY_YES' if edge_buy_yes >= edge_buy_no else 'BUY_NO'

        signal = {
            'sum_yes': float(sum_yes),
            'n_markets': n,
            'edge_buy_yes': float(edge_buy_yes),
            'edge_buy_no': float(edge_buy_no),
            'best_edge': float(best_edge),
        }

        label = f'group({self.relation_id[:16] or self._event_id[:16]})'

        # ── Close logic: check if we should exit existing positions ──
        if self._held_direction is not None:
            # Edge in our held direction (positive = still favorable)
            held_edge = (
                edge_buy_yes if self._held_direction == 'BUY_YES' else edge_buy_no
            )
            should_close = False
            close_reason = ''

            if held_edge < self.close_edge:
                # Edge gone — take profit or cut loss
                should_close = True
                close_reason = f'edge_gone held_edge={float(held_edge):.4f}'
            elif -held_edge > self.max_loss:
                # Edge reversed beyond max_loss — stop loss
                should_close = True
                close_reason = f'stop_loss held_edge={float(held_edge):.4f}'

            if should_close:
                await self._close_positions(trader, prices, label, signal, close_reason)
                return

            # Already positioned in this direction — don't add more
            return

        # ── Open logic: enter new position if edge is sufficient ──
        if best_edge < self.min_edge:
            return

        # Warmup & cooldown guard
        if self.is_warming_up():
            return
        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        size = compute_trade_size(
            trader.position_manager,
            best_edge,
            kelly_fraction=self.kelly_fraction,
            max_size=self.max_trade_size,
        )

        executed_legs = await self._open_positions(
            trader,
            prices,
            preferred_action,
            label,
            signal,
            size,
        )
        if executed_legs > 0:
            self._held_direction = preferred_action

    async def _open_positions(
        self,
        trader: Trader,
        prices: dict[str, Decimal],
        action: str,
        label: str,
        signal: dict,
        size: Decimal,
    ) -> int:
        """Place BUY orders on all legs. Returns number of executed legs."""
        n = len(prices)
        executed_legs = 0
        failed_legs = 0

        logger.info(
            'GroupArb: OPEN %s %s sum_yes=%.4f n=%d edge=%.4f size=%s',
            action,
            label,
            signal['sum_yes'],
            n,
            signal['best_edge'],
            size,
        )

        for mid, ask_price in prices.items():
            ticker = self._tickers[mid]

            if action == 'BUY_YES':
                trade_ticker = ticker
                leg_price = ask_price
            else:
                no_ticker = self._market_no_ticker.get(mid)
                if no_ticker is None:
                    no_ticker = trader.market_data.find_complement(ticker)
                if no_ticker is None:
                    logger.warning(
                        'GroupArb: no NO ticker for market %s, skipping leg',
                        mid[:16],
                    )
                    continue
                trade_ticker = no_ticker
                no_best_ask = trader.market_data.get_best_ask(no_ticker)
                if no_best_ask is not None:
                    leg_price = no_best_ask.price
                else:
                    leg_price = Decimal('1') - ask_price

            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=trade_ticker,
                    limit_price=leg_price,
                    quantity=size,
                )
                if result.failure_reason:
                    logger.warning('GroupArb leg failed: %s', result.failure_reason)
                    failed_legs += 1
                else:
                    executed_legs += 1
            except Exception:
                logger.exception(
                    'GroupArb: exception placing leg for market %s', mid[:16]
                )
                failed_legs += 1

        self.record_decision(
            ticker_name=label,
            action=action,
            executed=executed_legs > 0,
            reasoning=(
                f'OPEN sum_yes={signal["sum_yes"]:.4f} n={n} '
                f'edge={signal["best_edge"]:.4f} legs={executed_legs}/{n}'
            ),
            signal_values={**signal, 'executed_legs': executed_legs},
        )
        return executed_legs

    async def _close_positions(
        self,
        trader: Trader,
        prices: dict[str, Decimal],
        label: str,
        signal: dict,
        reason: str,
    ) -> None:
        """Sell all held positions to close."""
        closed_legs = 0
        failed_legs = 0
        direction = self._held_direction

        logger.info('GroupArb: CLOSE %s %s reason=%s', direction, label, reason)

        for mid in prices:
            ticker = self._tickers[mid]

            if direction == 'BUY_YES':
                # We hold YES — sell YES at bid
                sell_ticker = ticker
            else:
                # We hold NO — sell NO at bid
                no_ticker = self._market_no_ticker.get(mid)
                if no_ticker is None:
                    no_ticker = trader.market_data.find_complement(ticker)
                if no_ticker is None:
                    continue
                sell_ticker = no_ticker

            # Check we actually have a position to sell
            pos = trader.position_manager.get_position(sell_ticker)
            if pos is None or pos.quantity <= 0:
                continue

            best_bid = trader.market_data.get_best_bid(sell_ticker)
            if best_bid is None or best_bid.price <= 0:
                continue

            try:
                result = await trader.place_order(
                    side=TradeSide.SELL,
                    ticker=sell_ticker,
                    limit_price=best_bid.price,
                    quantity=pos.quantity,
                )
                if result.failure_reason:
                    failed_legs += 1
                else:
                    closed_legs += 1
            except Exception:
                logger.exception('GroupArb: close leg failed for %s', mid[:16])
                failed_legs += 1

        close_action = f'CLOSE_{direction}' if direction else 'CLOSE'
        self.record_decision(
            ticker_name=label,
            action=close_action,
            executed=closed_legs > 0,
            reasoning=f'{reason} closed={closed_legs} failed={failed_legs}',
            signal_values=signal,
        )

        if closed_legs > 0:
            self._held_direction = None
            self._last_arb_time = time.monotonic()
