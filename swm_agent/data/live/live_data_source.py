import asyncio
import json
import os
from typing import Any, Dict, Optional

import httpx

from ..data_source import DataSource


class LiveDataSource(DataSource):
    def __init__(
        self,
        event_cache_file: str = 'events_cache.jsonl',
        polling_interval: float = 60.0,
    ):
        self.event_cache_file = event_cache_file
        self.polling_interval = polling_interval
        self.processed_event_ids = set()
        self.event_queue = asyncio.Queue()

        if os.path.exists(self.event_cache_file):
            with open(self.event_cache_file, 'r') as f:
                for line in f:
                    event = json.loads(line.strip())
                    if 'id' in event:
                        self.processed_event_ids.add(str(event['id']))

    async def _fetch_events(self):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f'https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100'
            )

            if response.status_code == 200:
                events = response.json()
                return events
            return []

    async def _fetch_token_history(self, token_id):
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

    async def _fetch_market_history(self, event):
        modified_event = event.copy()

        for market in modified_event.get('markets', []):
            token_ids = json.loads(market.get('clobTokenIds', '[]'))
            market['history'] = {}

            for token_id in token_ids:
                history = await self._fetch_token_history(token_id)
                if history:
                    market['history'][token_id] = history

        return modified_event

    async def _poll_data(self):
        while True:
            try:
                events = await self._fetch_events()

                for event in events:
                    event_id = str(event.get('id'))
                    if event_id not in self.processed_event_ids:
                        enriched_event = await self._fetch_market_history(event)
                        await self.event_queue.put(enriched_event)
                        self.processed_event_ids.add(event_id)
                        with open(self.event_cache_file, 'a') as f:
                            f.write(json.dumps(enriched_event) + '\n')
            except Exception:
                pass

            await asyncio.sleep(self.polling_interval)

    async def start(self):
        asyncio.create_task(self._poll_data())

    async def get_next_event(self) -> Optional[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
