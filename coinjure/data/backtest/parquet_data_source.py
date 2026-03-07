from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq

from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.ticker import PolyMarketTicker

from ..data_source import DataSource

logger = logging.getLogger(__name__)

PMXT_BASE_URL = 'https://r2.pmxt.dev'


def download_pmxt_snapshot(hour_tag: str, dest_dir: str = 'data/') -> str:
    """Download a pmxt orderbook snapshot parquet file.

    Args:
        hour_tag: Hour tag, e.g. '2026-03-05T06'.
        dest_dir: Local directory to save to.

    Returns:
        Local path to the downloaded file.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    filename = f'polymarket_orderbook_{hour_tag}.parquet'
    local_path = dest / filename
    if local_path.exists():
        logger.info('Snapshot already exists: %s', local_path)
        return str(local_path)

    url = f'{PMXT_BASE_URL}/{filename}'
    logger.info('Downloading %s ...', url)
    with httpx.stream('GET', url, follow_redirects=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    logger.info('Saved to %s', local_path)
    return str(local_path)


class ParquetDataSource(DataSource):
    """Data source that reads real orderbook snapshots from pmxt parquet files.

    Each parquet row has: timestamp_received, timestamp_created_at, market_id,
    update_type ('price_change' or 'book_snapshot'), data (JSON string).

    Accepts a single path or a list of paths. Multiple files are concatenated
    and sorted by timestamp so that multi-hour backtests work seamlessly.
    """

    def __init__(
        self,
        parquet_path: str | list[str],
        market_id: str | None = None,
        market_ids: list[str] | None = None,
    ) -> None:
        if isinstance(parquet_path, list):
            self.parquet_paths = sorted(parquet_path)
        else:
            self.parquet_paths = [parquet_path]
        # Support single or multiple market IDs for filtering
        if market_ids:
            self._filter_ids: set[str] | None = set(market_ids)
        elif market_id:
            self._filter_ids = {market_id}
        else:
            self._filter_ids = None
        self._tickers: dict[tuple[str, str], PolyMarketTicker] = {}
        self.events: list[Event] = []
        self.index = 0
        self._load()

    def _get_or_create_ticker(
        self, token_id: str, market_id: str, side: str
    ) -> PolyMarketTicker:
        key = (token_id, market_id)
        existing = self._tickers.get(key)
        if existing is not None:
            return existing
        ticker = PolyMarketTicker(
            symbol=token_id,
            name=f'{side} ({market_id[:12]}...)',
            token_id=token_id,
            market_id=market_id,
        )
        self._tickers[key] = ticker
        return ticker

    def _load(self) -> None:
        import pyarrow as pa
        import pyarrow.compute as pc

        logger.info(
            'Loading %d parquet file(s): %s',
            len(self.parquet_paths),
            self.parquet_paths,
        )

        filter_expr = (
            pc.field('market_id').isin(list(self._filter_ids))
            if self._filter_ids
            else None
        )

        tables = []
        for p in self.parquet_paths:
            t = pq.read_table(p, filters=filter_expr)
            logger.info('  %s: %d rows', p, len(t))
            tables.append(t)

        table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        logger.info('Parquet total: %d rows after filter', len(table))

        rows = table.to_pydict()
        n_rows = len(rows['timestamp_received'])
        events: list[tuple[float, Event]] = []

        for i in range(n_rows):
            ts_received = rows['timestamp_received'][i]
            update_type = rows['update_type'][i]
            data_str = rows['data'][i]
            row_market_id = rows['market_id'][i] or ''

            ts_key = self._parse_timestamp_key(ts_received)

            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue

            token_id = data.get('token_id', '')
            side_label = data.get('side', 'YES')
            ticker = self._get_or_create_ticker(token_id, row_market_id, side_label)

            timestamp = self._parse_timestamp(ts_received)

            if update_type == 'book_snapshot':
                self._process_book_snapshot(data, ticker, timestamp, ts_key, events)
            elif update_type == 'price_change':
                self._process_price_change(data, ticker, timestamp, ts_key, events)

        events.sort(key=lambda pair: pair[0])
        self.events = [ev for _, ev in events]
        logger.info(
            'Parquet loaded: %d events from %d unique tickers',
            len(self.events),
            len(self._tickers),
        )

    def _process_book_snapshot(
        self,
        data: dict[str, Any],
        ticker: PolyMarketTicker,
        timestamp: datetime,
        ts_key: float,
        events: list[tuple[float, Event]],
    ) -> None:
        best_bid_str = data.get('best_bid')
        best_ask_str = data.get('best_ask')

        if best_bid_str is not None and best_ask_str is not None:
            try:
                mid = (Decimal(str(best_bid_str)) + Decimal(str(best_ask_str))) / 2
            except Exception:
                mid = None
            if mid is not None:
                events.append(
                    (
                        ts_key,
                        PriceChangeEvent(ticker=ticker, price=mid, timestamp=timestamp),
                    )
                )

        for bid in data.get('bids', []):
            if not isinstance(bid, list) or len(bid) < 2:
                continue
            price = Decimal(str(bid[0]))
            size = Decimal(str(bid[1]))
            events.append(
                (
                    ts_key,
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size,
                        side='bid',
                    ),
                )
            )

        for ask in data.get('asks', []):
            if not isinstance(ask, list) or len(ask) < 2:
                continue
            price = Decimal(str(ask[0]))
            size = Decimal(str(ask[1]))
            events.append(
                (
                    ts_key,
                    OrderBookEvent(
                        ticker=ticker,
                        price=price,
                        size=size,
                        size_delta=size,
                        side='ask',
                    ),
                )
            )

    def _process_price_change(
        self,
        data: dict[str, Any],
        ticker: PolyMarketTicker,
        timestamp: datetime,
        ts_key: float,
        events: list[tuple[float, Event]],
    ) -> None:
        best_bid_str = data.get('best_bid')
        best_ask_str = data.get('best_ask')
        if best_bid_str is None or best_ask_str is None:
            return

        try:
            best_bid = Decimal(str(best_bid_str))
            best_ask = Decimal(str(best_ask_str))
        except Exception:
            return

        mid = (best_bid + best_ask) / 2
        events.append(
            (
                ts_key,
                PriceChangeEvent(ticker=ticker, price=mid, timestamp=timestamp),
            )
        )

        change_price = data.get('change_price')
        change_size = data.get('change_size')
        change_side = data.get('change_side')
        if change_price is not None and change_size is not None and change_side:
            try:
                cp = Decimal(str(change_price))
                cs = Decimal(str(change_size))
            except Exception:
                return
            ob_side = 'bid' if str(change_side).upper() == 'BUY' else 'ask'
            events.append(
                (
                    ts_key,
                    OrderBookEvent(
                        ticker=ticker,
                        price=cp,
                        size=cs,
                        size_delta=cs,
                        side=ob_side,
                    ),
                )
            )

    @staticmethod
    def _parse_timestamp_key(value: Any) -> float:
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return float('inf')
            try:
                return float(raw)
            except ValueError:
                pass
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return float('inf')
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return float('inf')

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            raw = value.strip()
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except ValueError:
                pass
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc)

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None

    def drain_same_timestamp_events(self, first_event: Event) -> list[Event]:
        target_key = self._parse_timestamp_key(getattr(first_event, 'timestamp', None))
        if target_key == float('inf'):
            return []

        drained: list[Event] = []
        while self.index < len(self.events):
            candidate = self.events[self.index]
            candidate_key = self._parse_timestamp_key(
                getattr(candidate, 'timestamp', None)
            )
            if candidate_key != target_key:
                break
            drained.append(candidate)
            self.index += 1
        return drained
