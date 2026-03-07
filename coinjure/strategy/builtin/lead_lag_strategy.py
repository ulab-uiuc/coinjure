"""LeadLagStrategy — trade the follower when the leader moves.

For temporal relations where A leads B by N steps, when A makes a
significant move, B is expected to follow. We trade B in A's direction
before B catches up.

Entry: A moves by > entry_threshold from its recent mean.
  - A moved up → buy B (B will follow up)
  - A moved down → sell B / buy B's NO (B will follow down)
Exit: B has caught up (B's move matches A's), or timeout.

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/lead_lag_strategy.py:LeadLagStrategy \\
      --strategy-kwargs-json '{"relation_id": "610380-610379"}'
"""

from __future__ import annotations

import logging
import math
from collections import deque
from decimal import Decimal

from coinjure.engine.trader.trader import Trader
from coinjure.engine.trader.types import TradeSide
from coinjure.events import Event, PriceChangeEvent
from coinjure.market.relations import RelationStore
from coinjure.strategy.strategy import Strategy

logger = logging.getLogger(__name__)


class LeadLagStrategy(Strategy):
    """Trade the follower when the leader moves.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be a temporal type.
    trade_size:
        Dollar amount per trade.
    entry_threshold:
        Minimum move in A (as fraction, e.g. 0.03 = 3 cents) to trigger.
    warmup:
        Number of price observations to build A's baseline before trading.
    exit_reversion:
        Fraction of A's move that B must match to trigger exit (0.0-1.0).
    max_hold:
        Maximum number of B price updates to hold before forced exit.
    """

    name = 'lead_lag'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        trade_size: float = 10.0,
        entry_threshold: float = 0.03,
        warmup: int = 50,
        exit_reversion: float = 0.5,
        max_hold: int = 100,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.trade_size = Decimal(str(trade_size))
        self.entry_threshold = Decimal(str(entry_threshold))
        self._warmup_size = warmup
        self._exit_reversion = exit_reversion
        self._max_hold = max_hold

        self._relation = None
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)

        # Determine which is leader and which is follower.
        # lead_lag > 0: A leads B. lead_lag < 0: B leads A.
        if self._relation:
            lag = self._relation.lead_lag or 0
            if lag >= 0:
                # A leads B (or no lag)
                self._leader_id = self._relation.market_a.get(
                    'condition_id', ''
                ) or self._relation.market_a.get('id', '')
                self._follower_id = self._relation.market_b.get(
                    'condition_id', ''
                ) or self._relation.market_b.get('id', '')
            else:
                # B leads A — swap roles
                self._leader_id = self._relation.market_b.get(
                    'condition_id', ''
                ) or self._relation.market_b.get('id', '')
                self._follower_id = self._relation.market_a.get(
                    'condition_id', ''
                ) or self._relation.market_a.get('id', '')
        else:
            self._leader_id = ''
            self._follower_id = ''

        # Price tracking
        self._leader_prices: deque[float] = deque(maxlen=warmup)
        self._leader_price: Decimal | None = None
        self._follower_price: Decimal | None = None

        # Position state
        self._position_state = 'flat'  # flat | long_follower | short_follower
        self._entry_leader_price: float = 0.0  # leader price at entry
        self._entry_follower_price: float = 0.0  # follower price at entry
        self._hold_count = 0  # count of follower updates since entry

    def _matches(self, ticker_id: str, market_id: str) -> bool:
        if not market_id:
            return False
        return market_id in ticker_id or ticker_id in market_id

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused() or not isinstance(event, PriceChangeEvent):
            return

        ticker = event.ticker
        if ticker.symbol.endswith('_NO') or ticker.name.startswith('NO '):
            return

        tid = (
            getattr(ticker, 'market_id', '')
            or getattr(ticker, 'token_id', '')
            or ticker.symbol
        )

        is_leader = self._matches(tid, self._leader_id)
        is_follower = self._matches(tid, self._follower_id)

        if is_leader:
            self._leader_price = event.price
            self._leader_prices.append(float(event.price))
        elif is_follower:
            self._follower_price = event.price
            if self._position_state != 'flat':
                self._hold_count += 1
        else:
            return

        # Need warmup for leader baseline
        if len(self._leader_prices) < self._warmup_size:
            return
        if self._leader_price is None or self._follower_price is None:
            return

        leader_mean = sum(self._leader_prices) / len(self._leader_prices)
        leader_move = float(self._leader_price) - leader_mean

        if self._position_state == 'flat':
            if is_leader and abs(leader_move) > float(self.entry_threshold):
                if leader_move > 0:
                    await self._enter_long(trader, leader_move)
                else:
                    await self._enter_short(trader, leader_move)
            elif is_leader:
                self.record_decision(
                    ticker_name=f'lag({self.relation_id[:20]})',
                    action='HOLD',
                    executed=False,
                    reasoning=(
                        f'leader_move={leader_move:.4f} '
                        f'< threshold={float(self.entry_threshold):.4f}'
                    ),
                    signal_values={
                        'leader': float(self._leader_price),
                        'follower': float(self._follower_price),
                        'leader_mean': leader_mean,
                        'leader_move': leader_move,
                    },
                )
        else:
            # Check exit conditions on follower updates
            if is_follower:
                await self._check_exit(trader, leader_move)

    async def _enter_long(self, trader: Trader, leader_move: float) -> None:
        """Leader moved up → buy follower YES."""
        ticker_b = self._find_ticker(trader, self._follower_id, yes=True)
        if ticker_b and self._follower_price:
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b,
                limit_price=self._follower_price, quantity=self.trade_size,
            )

        self._position_state = 'long_follower'
        self._entry_leader_price = float(self._leader_price or 0)
        self._entry_follower_price = float(self._follower_price or 0)
        self._hold_count = 0

        self.record_decision(
            ticker_name=f'lag({self.relation_id[:20]})',
            action='BUY_FOLLOWER',
            executed=True,
            reasoning=f'Leader moved up {leader_move:.4f}, buying follower',
            signal_values={
                'leader': self._entry_leader_price,
                'follower': self._entry_follower_price,
                'leader_move': leader_move,
            },
        )
        logger.info('ENTER long follower: leader_move=%.4f', leader_move)

    async def _enter_short(self, trader: Trader, leader_move: float) -> None:
        """Leader moved down → sell follower (buy NO)."""
        ticker_b_no = self._find_ticker(trader, self._follower_id, yes=False)
        if ticker_b_no and self._follower_price:
            no_price = Decimal('1') - self._follower_price
            await trader.place_order(
                side=TradeSide.BUY, ticker=ticker_b_no,
                limit_price=no_price, quantity=self.trade_size,
            )

        self._position_state = 'short_follower'
        self._entry_leader_price = float(self._leader_price or 0)
        self._entry_follower_price = float(self._follower_price or 0)
        self._hold_count = 0

        self.record_decision(
            ticker_name=f'lag({self.relation_id[:20]})',
            action='SELL_FOLLOWER',
            executed=True,
            reasoning=f'Leader moved down {leader_move:.4f}, selling follower',
            signal_values={
                'leader': self._entry_leader_price,
                'follower': self._entry_follower_price,
                'leader_move': leader_move,
            },
        )
        logger.info('ENTER short follower: leader_move=%.4f', leader_move)

    async def _check_exit(self, trader: Trader, leader_move: float) -> None:
        """Exit if follower caught up or max hold exceeded."""
        follower_move = float(self._follower_price or 0) - self._entry_follower_price
        leader_at_entry_move = self._entry_leader_price - float(
            sum(self._leader_prices) / len(self._leader_prices)
        )

        # Follower caught up = follower moved in same direction by enough
        if abs(leader_at_entry_move) > 1e-6:
            catchup_ratio = follower_move / leader_at_entry_move
        else:
            catchup_ratio = 0.0

        should_exit = (
            catchup_ratio >= self._exit_reversion
            or self._hold_count >= self._max_hold
        )

        if should_exit:
            for pos in trader.position_manager.positions.values():
                if pos.quantity > 0:
                    best_bid = trader.market_data.get_best_bid(pos.ticker)
                    if best_bid:
                        await trader.place_order(
                            side=TradeSide.SELL, ticker=pos.ticker,
                            limit_price=best_bid.price, quantity=pos.quantity,
                        )

            reason = 'catchup' if catchup_ratio >= self._exit_reversion else 'timeout'
            self.record_decision(
                ticker_name=f'lag({self.relation_id[:20]})',
                action='EXIT',
                executed=True,
                reasoning=(
                    f'{reason}: follower_move={follower_move:.4f} '
                    f'catchup={catchup_ratio:.2f} hold={self._hold_count}'
                ),
                signal_values={
                    'follower_move': follower_move,
                    'catchup_ratio': catchup_ratio,
                    'hold_count': self._hold_count,
                },
            )
            logger.info('EXIT %s: %s catchup=%.2f', self._position_state, reason, catchup_ratio)
            self._position_state = 'flat'

    def _find_ticker(self, trader: Trader, market_id: str, yes: bool = True):
        for ticker in trader.market_data.order_books:
            is_no = (
                ticker.symbol.endswith('_NO')
                or ticker.name.startswith('NO ')
            )
            if yes and is_no:
                continue
            if not yes and not is_no:
                continue
            tid = (
                getattr(ticker, 'market_id', '')
                or getattr(ticker, 'token_id', '')
                or ticker.symbol
            )
            if self._matches(tid, market_id):
                return ticker
        return None
