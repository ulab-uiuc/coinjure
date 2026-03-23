import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any

import base64
import time as _time

import requests as _requests
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives import serialization as _serialization
from cryptography.hazmat.primitives.asymmetric import padding as _padding

from coinjure.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from coinjure.ticker import KalshiTicker

from ..source import DataSource

logger = logging.getLogger(__name__)

KALSHI_API_URL = 'https://api.elections.kalshi.com/trade-api/v2'


def _price_cents(d: dict[str, Any], field: str) -> int:
    """Extract a price in integer cents from a Kalshi market dict.

    Supports both the legacy format (``yes_bid``/``yes_ask`` as integer cents)
    and the newer API format where those fields are ``None`` and the actual
    values live in ``yes_bid_dollars``/``yes_ask_dollars`` as decimal strings.
    """
    val = d.get(field)
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    dollars_str = d.get(f'{field}_dollars', '0') or '0'
    try:
        return round(float(dollars_str) * 100)
    except (ValueError, TypeError):
        return 0


async def fetch_kalshi_price_history(
    series_ticker: str,
    market_ticker: str,
    period_interval: int = 60,
    lookback_days: int = 30,
    api_key_id: str | None = None,
    private_key_path: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch candlestick price history from Kalshi REST API.

    Returns a list of ``{t, p}`` dicts (same format as Polymarket's
    ``fetch_price_history``) for interoperability with the backtester.
    Uses the mid of yes_bid.close_dollars / yes_ask.close_dollars from
    each candlestick (new API format) with fallback to legacy int cents.

    URL: /series/{series_ticker}/markets/{market_ticker}/candlesticks
    """
    import datetime

    import httpx

    now = datetime.datetime.now(datetime.timezone.utc)
    start_ts = int((now - datetime.timedelta(days=lookback_days)).timestamp())
    end_ts = int(now.timestamp())

    path = f'/trade-api/v2/series/{series_ticker}/markets/{market_ticker}/candlesticks'
    url = f'https://api.elections.kalshi.com{path}'
    params = {
        'period_interval': period_interval,
        'start_ts': start_ts,
        'end_ts': end_ts,
    }

    # Build auth headers using RSA signing
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    auth_headers: dict[str, str] = {}
    if key_id and pk_path and os.path.exists(pk_path):
        try:
            ts = str(int(_time.time() * 1000))
            msg = ts + 'GET' + path
            with open(pk_path, 'rb') as f:
                pk = _serialization.load_pem_private_key(f.read(), password=None)
            sig = base64.b64encode(
                pk.sign(msg.encode(), _padding.PKCS1v15(), _hashes.SHA256())
            ).decode()
            auth_headers = {
                'KALSHI-ACCESS-KEY': key_id,
                'KALSHI-ACCESS-TIMESTAMP': ts,
                'KALSHI-ACCESS-SIGNATURE': sig,
            }
        except Exception as e:
            logger.warning('Could not sign Kalshi candlestick request: %s', e)

    max_retries = 5
    base_delay = 3.0
    resp = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            resp = await client.get(url, params=params, headers=auth_headers)
            if resp.status_code == 429:
                delay = base_delay * (2**attempt)
                logger.warning(
                    'Kalshi 429 rate-limited (%s), retry %d/%d in %.0fs',
                    market_ticker,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    if resp is None or resp.status_code != 200:
        logger.warning(
            'Kalshi candlestick %s: HTTP %s %s',
            market_ticker,
            resp.status_code if resp else 'N/A',
            resp.text[:200] if resp else '',
        )
        return []

    def _candle_price(side_dict: dict | None, field: str) -> float | None:
        """Extract price from candle side dict, supporting dollars or int-cents."""
        if not side_dict:
            return None
        dollars = side_dict.get(f'{field}_dollars')
        if dollars is not None:
            try:
                return float(dollars)
            except (ValueError, TypeError):
                pass
        cents = side_dict.get(field)
        if cents is not None:
            try:
                return int(cents) / 100
            except (ValueError, TypeError):
                pass
        return None

    data = resp.json()
    result: list[dict[str, Any]] = []
    for candle in data.get('candlesticks', []):
        ts = candle.get('end_period_ts')
        if ts is None:
            continue
        bid_close = _candle_price(candle.get('yes_bid'), 'close')
        ask_close = _candle_price(candle.get('yes_ask'), 'close')
        if bid_close is None and ask_close is None:
            price_dict = candle.get('price') or {}
            price_prev = _candle_price(price_dict, 'previous')
            if price_prev is None:
                continue
            mid = price_prev
        else:
            b = bid_close if bid_close is not None else ask_close
            a = ask_close if ask_close is not None else bid_close
            mid = (b + a) / 2  # already in 0-1 range
        result.append({'t': ts, 'p': mid})
    return result


async def _retry_with_backoff(
    func,
    *args,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    **kwargs,
):
    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt >= max_attempts - 1:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                'Attempt %d/%d failed: %s. Retrying in %.1fs',
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)


class LiveKalshiDataSource(DataSource):
    """Polls Kalshi REST API for markets, uses market-level bid/ask data."""

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        event_cache_file: str = 'kalshi_events_cache.jsonl',
        polling_interval: float = 60.0,
        reprocess_on_start: bool = True,
        watch_series: list[str] | None = None,
        watch_events: list[str] | None = None,
    ):
        self.polling_interval = polling_interval
        self._watch_series = watch_series or []
        self._watch_events = watch_events or []
        self.event_cache_file = event_cache_file
        self.processed_event_tickers: set[str] = set()
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.last_prices: dict[str, tuple[int, int]] = {}
        self._news_fetched_events: set[str] = set()
        self._poll_task: asyncio.Task | None = None

        # Setup Kalshi API client
        from kalshi_python import Configuration
        from kalshi_python.api.markets_api import MarketsApi
        from kalshi_python.api_client import ApiClient

        config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')

        key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
        pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')

        self._api_client = ApiClient(configuration=config)
        if key_id and pk_path:
            self._api_client.set_kalshi_auth(key_id, pk_path)
        else:
            logger.warning(
                'Kalshi API credentials not provided. '
                'Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars.'
            )

        self._markets_api = MarketsApi(self._api_client)

        # Cache private key for raw HTTP signing
        self._key_id = key_id
        self._private_key = None
        if pk_path and os.path.exists(pk_path):
            try:
                with open(pk_path, 'rb') as f:
                    self._private_key = _serialization.load_pem_private_key(f.read(), password=None)
            except Exception as e:
                logger.warning('Could not load Kalshi private key for raw HTTP: %s', e)

        # Load cache
        if os.path.exists(self.event_cache_file):
            with open(self.event_cache_file) as f:
                for line in f:
                    try:
                        cached = json.loads(line.strip())
                        if 'event_ticker' in cached:
                            # Always track which events we've already sent news for,
                            # to avoid re-triggering LLM calls on restart.
                            self._news_fetched_events.add(cached['event_ticker'])
                            if not reprocess_on_start:
                                self.processed_event_tickers.add(cached['event_ticker'])
                    except json.JSONDecodeError:
                        pass

    def _raw_kalshi_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make an authenticated raw HTTP GET to the Kalshi API."""
        base_path = path.split('?')[0]
        ts = str(int(_time.time() * 1000))
        msg = ts + 'GET' + base_path
        if self._private_key is not None:
            sig = self._private_key.sign(msg.encode(), _padding.PKCS1v15(), _hashes.SHA256())
            sig_b64 = base64.b64encode(sig).decode()
        else:
            sig_b64 = ''
        headers = {
            'KALSHI-ACCESS-KEY': self._key_id or '',
            'KALSHI-ACCESS-TIMESTAMP': ts,
            'KALSHI-ACCESS-SIGNATURE': sig_b64,
        }
        r = _requests.get(
            f'https://api.elections.kalshi.com{path}',
            params=params,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    async def _fetch_markets(self) -> list[dict[str, Any]]:
        """Fetch open markets from Kalshi API using raw HTTP (SDK omits price fields)."""
        try:
            all_markets: list[dict[str, Any]] = []
            seen_tickers: set[str] = set()
            cursor = None

            for _ in range(5):
                params: dict[str, Any] = {'status': 'open', 'limit': 200}
                if cursor:
                    params['cursor'] = cursor
                data = await asyncio.to_thread(
                    lambda p=params: self._raw_kalshi_get('/trade-api/v2/markets', p)
                )
                raw_markets: list[dict[str, Any]] = data.get('markets', [])
                for d in raw_markets:
                    yes_ask = _price_cents(d, 'yes_ask')
                    if yes_ask == 0:
                        continue
                    tkr = d.get('ticker', '')
                    if tkr and tkr not in seen_tickers:
                        seen_tickers.add(tkr)
                        all_markets.append(d)

                cursor = data.get('cursor')
                if not cursor or len(raw_markets) < 200:
                    break
                await asyncio.sleep(0.3)

            # Also fetch markets from explicitly watched series
            for series in self._watch_series:
                try:
                    data = await asyncio.to_thread(
                        lambda s=series: self._raw_kalshi_get(
                            '/trade-api/v2/markets',
                            {'series_ticker': s, 'status': 'open', 'limit': 100},
                        )
                    )
                    for d in data.get('markets', []):
                        tkr = d.get('ticker', '')
                        if tkr and tkr not in seen_tickers:
                            seen_tickers.add(tkr)
                            all_markets.append(d)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.debug('Could not fetch series %s: %s', series, e)

            # Also fetch markets from explicitly watched events (event_ticker queries)
            for event_ticker in self._watch_events:
                try:
                    data = await asyncio.to_thread(
                        lambda et=event_ticker: self._raw_kalshi_get(
                            '/trade-api/v2/markets',
                            {'event_ticker': et, 'status': 'open', 'limit': 100},
                        )
                    )
                    for d in data.get('markets', []):
                        tkr = d.get('ticker', '')
                        if tkr and tkr not in seen_tickers:
                            seen_tickers.add(tkr)
                            all_markets.append(d)
                    await asyncio.sleep(0.2)
                except Exception as e:
                    logger.debug('Could not fetch event %s: %s', event_ticker, e)

            return all_markets
        except Exception as e:
            logger.error('Error fetching Kalshi markets: %s', e)
            return []

    def _market_to_order_book_events(
        self,
        market: dict[str, Any],
    ) -> list[OrderBookEvent]:
        """Create OrderBookEvents from market-level yes_bid/yes_ask data.

        Kalshi's bulk market API includes top-of-book prices (in cents).
        We use these directly instead of fetching individual orderbooks.
        """
        events = []
        market_ticker = market.get('ticker', '')
        market_title = market.get('title', '')
        event_ticker = market.get('event_ticker', '')
        series_ticker = market.get('series_ticker', '')

        yes_bid = _price_cents(market, 'yes_bid')
        yes_ask = _price_cents(market, 'yes_ask')

        ticker_yes = KalshiTicker(
            symbol=market_ticker,
            name=market_title,
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
        )
        ticker_no = KalshiTicker(
            symbol=f'{market_ticker}_NO',
            name=f'{market_title} (NO)',
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            side='no',
        )

        prev = self.last_prices.get(market_ticker, (0, 0))
        prev_bid, prev_ask = prev

        # Synthetic size for top-of-book
        size = Decimal('100')

        # YES side: emit bid/ask events
        if yes_bid > 0 and yes_bid != prev_bid:
            bid_price = Decimal(str(yes_bid)) / Decimal('100')
            events.append(
                OrderBookEvent(
                    ticker=ticker_yes,
                    price=bid_price,
                    size=size,
                    size_delta=size,
                    side='bid',
                )
            )

        if yes_ask > 0 and yes_ask != prev_ask:
            ask_price = Decimal(str(yes_ask)) / Decimal('100')
            events.append(
                OrderBookEvent(
                    ticker=ticker_yes,
                    price=ask_price,
                    size=size,
                    size_delta=size,
                    side='ask',
                )
            )

        # NO side: derive from YES prices (no_bid = 1 - yes_ask, no_ask = 1 - yes_bid)
        if yes_ask > 0 and yes_ask != prev_ask:
            no_bid_price = Decimal('1') - Decimal(str(yes_ask)) / Decimal('100')
            events.append(
                OrderBookEvent(
                    ticker=ticker_no,
                    price=no_bid_price,
                    size=size,
                    size_delta=size,
                    side='bid',
                )
            )

        if yes_bid > 0 and yes_bid != prev_bid:
            no_ask_price = Decimal('1') - Decimal(str(yes_bid)) / Decimal('100')
            events.append(
                OrderBookEvent(
                    ticker=ticker_no,
                    price=no_ask_price,
                    size=size,
                    size_delta=size,
                    side='ask',
                )
            )

        self.last_prices[market_ticker] = (yes_bid, yes_ask)

        # Derive PriceChangeEvent from mid-price (YES side only)
        if yes_bid > 0 and yes_ask > 0:
            mid = (Decimal(str(yes_bid)) + Decimal(str(yes_ask))) / Decimal('200')
        elif yes_bid > 0:
            mid = Decimal(str(yes_bid)) / Decimal('100')
        elif yes_ask > 0:
            mid = Decimal(str(yes_ask)) / Decimal('100')
        else:
            mid = None

        if mid is not None and (yes_bid != prev_bid or yes_ask != prev_ask):
            events.append(PriceChangeEvent(ticker=ticker_yes, price=mid))

        return events

    async def _fetch_and_emit_news(
        self,
        market_question: str,
        event_ticker: str,
        ticker: KalshiTicker,
    ) -> None:
        """Emit market title as NewsEvent for strategy consumption."""
        try:
            news_event = NewsEvent(
                news=market_question,
                title=market_question,
                source='kalshi',
                description=market_question,
                event_id=event_ticker,
                ticker=ticker,
            )
            await self.event_queue.put(news_event)
        except Exception as e:
            logger.warning('News emit error for "%s": %s', market_question[:50], e)

    async def _poll_data(self) -> None:
        while True:
            try:
                markets = await _retry_with_backoff(self._fetch_markets)
                logger.info('Fetched %d liquid Kalshi markets', len(markets))

                new_event_tickers: set[str] = set()
                news_queue: list[tuple[str, str, KalshiTicker]] = []

                for market in markets:
                    market_ticker = market.get('ticker', '')
                    event_ticker = market.get('event_ticker', '')
                    market_title = market.get('title', '')

                    if not market_ticker:
                        continue

                    is_new = (
                        event_ticker
                        and event_ticker not in self.processed_event_tickers
                    )

                    if is_new:
                        new_event_tickers.add(event_ticker)
                        self.processed_event_tickers.add(event_ticker)
                        with open(self.event_cache_file, 'a') as f:
                            f.write(
                                json.dumps(
                                    {
                                        'event_ticker': event_ticker,
                                        'market_ticker': market_ticker,
                                        'title': market_title,
                                    }
                                )
                                + '\n'
                            )

                    # Use market-level bid/ask to create order book events
                    ob_events = self._market_to_order_book_events(market)

                    for ob_event in ob_events:
                        await self.event_queue.put(ob_event)

                    has_ask = _price_cents(market, 'yes_ask') > 0

                    # Queue news fetch for new events (once per event)
                    if (
                        event_ticker in new_event_tickers
                        and event_ticker not in self._news_fetched_events
                        and has_ask
                    ):
                        self._news_fetched_events.add(event_ticker)
                        tkr = KalshiTicker(
                            symbol=market_ticker,
                            name=market_title,
                            market_ticker=market_ticker,
                            event_ticker=event_ticker,
                            series_ticker=market.get('series_ticker', ''),
                        )
                        news_queue.append((market_title, event_ticker, tkr))

                # Emit news for up to 5 new markets per poll
                if news_queue:
                    batch = news_queue[:5]
                    logger.info(
                        'Emitting news for %d/%d new Kalshi markets...',
                        len(batch),
                        len(news_queue),
                    )
                    for question, evt_ticker, tkr in batch:
                        await self._fetch_and_emit_news(
                            market_question=question,
                            event_ticker=evt_ticker,
                            ticker=tkr,
                        )

            except Exception as e:
                logger.error('Error in Kalshi polling loop: %s', e)

            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_data())

    async def stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def get_next_event(self) -> Event | None:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
