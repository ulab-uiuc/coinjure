#!/usr/bin/env python3
"""
FMP API Client for PEAD Screener

Provides rate-limited access to Financial Modeling Prep API endpoints
for PEAD (Post-Earnings Announcement Drift) screening.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- API call budget enforcement
- Batch company profile support
- Earnings calendar and historical price fetching
"""

import os
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    print(
        'ERROR: requests library not found. Install with: pip install requests',
        file=sys.stderr,
    )
    sys.exit(1)


class ApiCallBudgetExceeded(Exception):
    """Raised when the API call budget has been exhausted."""

    pass


class FMPClient:
    """Client for Financial Modeling Prep API with rate limiting, caching, and budget control"""

    BASE_URL = 'https://financialmodelingprep.com/api/v3'
    RATE_LIMIT_DELAY = 0.3  # 300ms between requests

    def __init__(self, api_key: Optional[str] = None, max_api_calls: int = 200):
        self.api_key = api_key or os.getenv('FMP_API_KEY')
        if not self.api_key:
            raise ValueError(
                'FMP API key required. Set FMP_API_KEY environment variable '
                'or pass api_key parameter.'
            )
        self.session = requests.Session()
        self.cache = {}
        self.last_call_time = 0
        self.rate_limit_reached = False
        self.retry_count = 0
        self.max_retries = 1
        self.api_calls_made = 0
        self.max_api_calls = max_api_calls

    def _rate_limited_get(
        self, url: str, params: Optional[dict] = None
    ) -> Optional[dict]:
        """Make a rate-limited GET request with budget enforcement.

        Raises:
            ApiCallBudgetExceeded: When api_calls_made >= max_api_calls
        """
        if self.api_calls_made >= self.max_api_calls:
            raise ApiCallBudgetExceeded(
                f'API call budget exhausted: {self.api_calls_made}/{self.max_api_calls} calls used'
            )

        if self.rate_limit_reached:
            return None

        if params is None:
            params = {}
        params['apikey'] = self.api_key

        elapsed = time.time() - self.last_call_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)

        try:
            response = self.session.get(url, params=params, timeout=30)
            self.last_call_time = time.time()
            self.api_calls_made += 1

            if response.status_code == 200:
                self.retry_count = 0
                return response.json()
            elif response.status_code == 429:
                self.retry_count += 1
                if self.retry_count <= self.max_retries:
                    print(
                        'WARNING: Rate limit exceeded. Waiting 60 seconds...',
                        file=sys.stderr,
                    )
                    time.sleep(60)
                    return self._rate_limited_get(url, params)
                else:
                    print('ERROR: Daily API rate limit reached.', file=sys.stderr)
                    self.rate_limit_reached = True
                    return None
            else:
                print(
                    f'ERROR: API request failed: {response.status_code} - {response.text[:200]}',
                    file=sys.stderr,
                )
                return None
        except requests.exceptions.Timeout:
            print('ERROR: Request timed out', file=sys.stderr)
            return None
        except requests.exceptions.RequestException as e:
            print(f'ERROR: Request exception: {e}', file=sys.stderr)
            return None

    def get_earnings_calendar(
        self, from_date: str, to_date: str
    ) -> Optional[list[dict]]:
        """Fetch earnings calendar for a date range.

        Args:
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format

        Returns:
            List of earnings event dicts or None on failure.
            Each dict contains: date, symbol, eps, epsEstimated, revenue,
            revenueEstimated, time (bmo/amc)
        """
        cache_key = f'earnings_{from_date}_{to_date}'
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = f'{self.BASE_URL}/earning_calendar'
        params = {'from': from_date, 'to': to_date}
        data = self._rate_limited_get(url, params)
        if data:
            self.cache[cache_key] = data
        return data

    def get_company_profiles(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch company profiles for multiple symbols in batches.

        Args:
            symbols: List of stock symbols

        Returns:
            Dict mapping symbol -> profile dict (with marketCap, sector, etc.)
        """
        results = {}
        batch_size = 100
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch_str = ','.join(batch)

            cache_key = f'profile_{batch_str}'
            if cache_key in self.cache:
                for profile in self.cache[cache_key]:
                    results[profile['symbol']] = profile
                continue

            url = f'{self.BASE_URL}/profile/{batch_str}'
            data = self._rate_limited_get(url)
            if data:
                self.cache[cache_key] = data
                for profile in data:
                    results[profile['symbol']] = profile
        return results

    def get_historical_prices(self, symbol: str, days: int = 90) -> Optional[dict]:
        """Fetch historical daily OHLCV data.

        Args:
            symbol: Stock symbol
            days: Number of trading days to fetch

        Returns:
            Dict with 'symbol' and 'historical' keys, where 'historical' is a
            list of price dicts (most-recent-first) with: date, open, high, low,
            close, adjClose, volume
        """
        cache_key = f'prices_{symbol}_{days}'
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = f'{self.BASE_URL}/historical-price-full/{symbol}'
        params = {'timeseries': days}
        data = self._rate_limited_get(url, params)
        if data:
            self.cache[cache_key] = data
        return data

    def get_api_stats(self) -> dict:
        """Return API usage statistics."""
        return {
            'cache_entries': len(self.cache),
            'api_calls_made': self.api_calls_made,
            'max_api_calls': self.max_api_calls,
            'rate_limit_reached': self.rate_limit_reached,
            'budget_remaining': max(0, self.max_api_calls - self.api_calls_made),
        }
