import asyncio
import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import httpx
from py_clob_client.client import ClobClient

from ...events.events import Event, NewsEvent, OrderBookEvent
from ...ticker.ticker import PolyMarketTicker, Ticker
from ..data_source import DataSource


class LivePolyMarketDataSource(DataSource):
    event_cache_file: str
    polling_interval: float
    processed_event_ids: Set[str]
    event_queue: asyncio.Queue
    clob_client: ClobClient
    last_order_book_state: Dict[str, Decimal]

    def __init__(
        self,
        event_cache_file: str = 'events_cache.jsonl',
        polling_interval: float = 60.0,
    ):
        self.event_cache_file = event_cache_file
        self.polling_interval = polling_interval
        self.processed_event_ids = set()
        self.event_queue = asyncio.Queue()
        self.clob_client = ClobClient('https://clob.polymarket.com')
        self.last_order_book_state = {}

        if os.path.exists(self.event_cache_file):
            with open(self.event_cache_file, 'r') as f:
                for line in f:
                    event = json.loads(line.strip())
                    if 'id' in event:
                        self.processed_event_ids.add(str(event['id']))

    async def _fetch_events(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f'https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100'
            )
            if response.status_code == 200:
                events = response.json()
                return events
            return []

    async def _fetch_token_history(self, token_id: str) -> List[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f'https://clob.polymarket.com/price-history?tokenId={token_id}&fidelity=60&interval=max'
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get('history', [])
            return []
        except Exception:
            return []

    async def _fetch_market_history(self, event: Dict[str, Any]) -> Dict[str, Any]:
        modified_event = event.copy()
        for market in modified_event.get('markets', []):
            token_ids = json.loads(market.get('clobTokenIds', '[]'))
            market['history'] = {}
            for token_id in token_ids:
                history = await self._fetch_token_history(token_id)
                if history:
                    market['history'][token_id] = history
        return modified_event

    def _fetch_order_book(self, token_id: str) -> Any:
        try:
            order_book = self.clob_client.get_order_book(token_id)
            return order_book
        except Exception as e:
            print(f'Error fetching order book for {token_id}: {e}')
            return None

    def _process_order_book_to_events(
        self,
        token_id: str,
        market_title: str,
        order_book: Any,
        market_id: str = '',
        event_id: str = '',
    ) -> List[OrderBookEvent]:
        events = []

        if not order_book:
            return events

        # Create PolyMarketTicker with all available IDs
        ticker = PolyMarketTicker(
            symbol=token_id,
            name=market_title,
            token_id=token_id,
            market_id=market_id,
            event_id=event_id,
        )

        for bid in order_book.bids:
            price = Decimal(bid.price)
            size = Decimal(bid.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{bid.price}:bid'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker, price=price, size=size, size_delta=size_delta
                )
                events.append(event)

        for ask in order_book.asks:
            price = Decimal(ask.price)
            size = Decimal(ask.size)

            prev_size = Decimal('0')
            prev_key = f'{token_id}:{ask.price}:ask'
            if prev_key in self.last_order_book_state:
                prev_size = self.last_order_book_state[prev_key]

            size_delta = size - prev_size

            self.last_order_book_state[prev_key] = size

            if size_delta != 0:
                event = OrderBookEvent(
                    ticker=ticker, price=price, size=size, size_delta=size_delta
                )
                events.append(event)

        return events

    async def _poll_data(self) -> None:
        while True:
            try:
                events = await self._fetch_events()
                for event in events:
                    event_id = str(event.get('id'))
                    if event_id not in self.processed_event_ids:
                        enriched_event = await self._fetch_market_history(event)

                        news_content = f"{enriched_event.get('title', '')}: {enriched_event.get('description', '')}"
                        news_event = NewsEvent(news=news_content)
                        await self.event_queue.put(news_event)

                        for market in enriched_event.get('markets', []):
                            market_title = market.get('title', '')
                            market_id = market.get('id', '')
                            token_ids = json.loads(market.get('clobTokenIds', '[]'))

                            for token_id in token_ids:
                                order_book = self._fetch_order_book(token_id)

                                order_book_events = self._process_order_book_to_events(
                                    token_id,
                                    market_title,
                                    order_book,
                                    market_id=market_id,
                                    event_id=event_id,
                                )

                                for ob_event in order_book_events:
                                    await self.event_queue.put(ob_event)

                        self.processed_event_ids.add(event_id)
                        with open(self.event_cache_file, 'a') as f:
                            f.write(json.dumps(enriched_event) + '\n')
            except Exception as e:
                print(f'Error in polling loop: {e}')

            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        asyncio.create_task(self._poll_data())

    async def get_next_event(self) -> Optional[Event]:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None


class LiveNewsDataSource(DataSource):
    api_token: str
    cache_file: str
    polling_interval: float
    max_articles_per_poll: int
    languages: List[str]
    categories: List[str]
    base_url: str
    processed_article_ids: Set[str]
    event_queue: asyncio.Queue

    def __init__(
        self,
        api_token: str,
        cache_file: str = 'news_cache.jsonl',
        polling_interval: float = 300.0,
        max_articles_per_poll: int = 10,
        languages: List[str] = None,
        categories: List[str] = None,
    ):
        self.api_token = api_token
        self.cache_file = cache_file
        self.polling_interval = polling_interval
        self.max_articles_per_poll = max_articles_per_poll
        self.languages = languages or ['en']
        self.categories = categories or []
        self.base_url = 'https://api.thenewsapi.com/v1/news/headlines'

        self.processed_article_ids = set()
        self.event_queue = asyncio.Queue()

        self._load_processed_articles()

    def _load_processed_articles(self) -> None:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    for line in f:
                        article = json.loads(line.strip())
                        if 'uuid' in article:
                            self.processed_article_ids.add(article['uuid'])
            except Exception as e:
                print(f'Error loading article cache: {e}')

    async def _fetch_articles(self) -> List[Dict[str, Any]]:
        try:
            today = datetime.now()

            params = {
                'api_token': self.api_token,
                'language': ','.join(self.languages),
                'published_on': today.strftime('%Y-%m-%d'),
                'locale': 'us',
                'limit': self.max_articles_per_poll,
            }

            if self.categories:
                params['categories'] = ','.join(self.categories)

            async with httpx.AsyncClient() as client:
                response = await client.get(self.base_url, params=params)

                if response.status_code == 200:
                    try:
                        # Print raw response for debugging
                        print(f'Raw API response: {response.text[:500]}...')

                        # Try to parse the response as JSON
                        data = response.json()

                        # Verify that data is a dictionary
                        if not isinstance(data, dict):
                            print(f'API returned non-dictionary response: {type(data)}')
                            return []

                        # Check if 'data' key exists
                        if 'data' not in data:
                            print(
                                f"API response missing 'data' key. Available keys: {list(data.keys())}"
                            )

                            # Try to extract articles from other possible structures
                            if 'articles' in data:
                                print("Found 'articles' key instead of 'data'")
                                articles = data.get('articles', [])
                                if isinstance(articles, list):
                                    return articles

                            # If this looks like it might be a single article
                            if 'title' in data or 'url' in data:
                                print('Response appears to be a single article')
                                return [data]

                            # Try other potential formats
                            for key, value in data.items():
                                if isinstance(value, list):
                                    print(
                                        f"Found list under key '{key}', trying this instead"
                                    )
                                    return value

                            return []

                        articles_data = data.get('data', [])

                        # Check if articles_data is a list
                        if isinstance(articles_data, list):
                            return articles_data

                        # If articles_data is a dict, it might be organized by categories
                        if isinstance(articles_data, dict):
                            print(f"'data' is a dictionary, attempting to flatten")
                            flattened_articles = []

                            for category, articles in articles_data.items():
                                if isinstance(articles, list):
                                    print(
                                        f"Found {len(articles)} articles in category '{category}'"
                                    )
                                    flattened_articles.extend(articles)
                                elif isinstance(articles, dict):
                                    print(
                                        f"Category '{category}' contains a dictionary, not a list"
                                    )
                                    flattened_articles.append(
                                        articles
                                    )  # Add as a single article

                            print(
                                f'Flattened {len(flattened_articles)} articles from categories'
                            )
                            return flattened_articles

                        print(f"Unexpected 'data' format: {type(articles_data)}")
                        return []

                    except json.JSONDecodeError as e:
                        print(f'Failed to parse API response as JSON: {e}')
                        print(f'Raw response: {response.text[:200]}...')
                        return []
                else:
                    print(f'API error: HTTP {response.status_code}')
                    print(f'Response: {response.text[:200]}...')
                    return []
        except Exception as e:
            print(f'Error fetching articles: {e}')
            import traceback

            traceback.print_exc()
            return []

    def _create_news_event(self, article: Dict[str, Any]) -> NewsEvent:
        # Handle published_at date
        published_at = None
        if 'published_at' in article and article['published_at']:
            try:
                published_at_str = str(article['published_at'])
                if 'Z' in published_at_str:
                    published_at = datetime.fromisoformat(
                        published_at_str.replace('Z', '+00:00')
                    )
                else:
                    published_at = datetime.fromisoformat(published_at_str)
            except (ValueError, TypeError) as e:
                print(f"Error parsing date '{article.get('published_at')}': {e}")
                published_at = datetime.now()
        else:
            published_at = datetime.now()

        # Extract title safely
        title = ''
        if 'title' in article and article['title']:
            title = str(article['title'])

        # Extract description safely
        description = ''
        if 'description' in article and article['description']:
            description = str(article['description'])
        elif 'snippet' in article and article['snippet']:
            description = str(article['snippet'])

        # Create news content
        news_content = f'{title}: {description}' if description else title
        if not news_content:
            news_content = 'No content available'

        # Extract categories safely
        categories = []
        if 'categories' in article and isinstance(article['categories'], list):
            categories = article['categories']

        # Create the event
        return NewsEvent(
            news=news_content,
            title=title,
            source=str(article.get('source', '')),
            url=str(article.get('url', '')),
            published_at=published_at,
            categories=categories,
            description=description,
            image_url=str(article.get('image_url', '')),
            uuid=str(article.get('uuid', '')),
            event_id=str(article.get('uuid', '')),
        )

    async def _retry_on_error(
        self, func, *args, retries: int = 3, delay: int = 2, **kwargs
    ) -> Any:
        for attempt in range(retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                print(f'Attempt {attempt + 1} failed: {e}')
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise

    def _save_article(self, article: Dict[str, Any]) -> None:
        try:
            with open(self.cache_file, 'a', encoding='utf-8') as f:
                json.dump(article, f, ensure_ascii=False)
                f.write('\n')
        except Exception as e:
            print(f'Error saving article: {e}')

    async def _poll_data(self) -> None:
        while True:
            try:
                print(f'Fetching news articles...')
                articles = await self._retry_on_error(self._fetch_articles)
                print(f'Received {len(articles)} articles')

                for article in articles:
                    try:
                        # Verify article is a dictionary
                        if not isinstance(article, dict):
                            print(f'Skipping non-dictionary article: {type(article)}')
                            continue

                        # Check for UUID
                        article_id = article.get('uuid')
                        if not article_id:
                            print(f'Article missing UUID, generating random ID')
                            article_id = str(uuid.uuid4())
                            article['uuid'] = article_id

                        # Skip processed articles
                        if article_id in self.processed_article_ids:
                            print(f'Skipping already processed article: {article_id}')
                            continue

                        # Create news event
                        event = self._create_news_event(article)

                        # Add to queue
                        await self.event_queue.put(event)
                        print(
                            f"Added article to queue: {article_id} - {article.get('title', 'No title')}"
                        )

                        # Mark as processed and save
                        self.processed_article_ids.add(article_id)
                        self._save_article(article)

                    except Exception as e:
                        print(f'Error processing article: {e}')
                        continue

            except Exception as e:
                print(f'Error in polling loop: {e}')
                # Add traceback for better debugging
                import traceback

                traceback.print_exc()

            print(f'Sleeping for {self.polling_interval} seconds')
            await asyncio.sleep(self.polling_interval)

    async def start(self) -> None:
        asyncio.create_task(self._poll_data())

    async def get_next_event(self) -> Optional[NewsEvent]:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
