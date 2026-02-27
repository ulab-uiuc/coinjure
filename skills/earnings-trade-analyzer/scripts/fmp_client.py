#!/usr/bin/env python3
"""
FMP API Client for Earnings Trade Analyzer

Provides rate-limited access to Financial Modeling Prep API endpoints
for post-earnings trade analysis and scoring.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- API call budget enforcement
- Batch profile support
- Earnings calendar fetching
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
    US_EXCHANGES = ['NYSE', 'NASDAQ', 'AMEX', 'NYSEArca', 'BATS', 'NMS', 'NGM', 'NCM']

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
        """Execute a rate-limited GET request with budget enforcement."""
        if self.rate_limit_reached:
            return None

        if self.api_calls_made >= self.max_api_calls:
            raise ApiCallBudgetExceeded(
                f'API call budget exceeded: {self.api_calls_made}/{self.max_api_calls} calls used.'
            )

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
            print(f'WARNING: Request timed out for {url}', file=sys.stderr)
            return None
        except requests.exceptions.RequestException as e:
            print(f'ERROR: Request exception: {e}', file=sys.stderr)
            return None

    def get_earnings_calendar(self, from_date: str, to_date: str) -> Optional[list]:
        """Fetch earnings calendar for a date range.

        Args:
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)

        Returns:
            List of earnings announcements or None on failure.
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
        """Fetch company profiles for multiple symbols in batches of 100.

        Args:
            symbols: List of ticker symbols

        Returns:
            Dictionary mapping symbol to profile data.
        """
        profiles = {}
        batch_size = 100

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            symbols_str = ','.join(batch)

            cache_key = f'profiles_{symbols_str}'
            if cache_key in self.cache:
                for profile in self.cache[cache_key]:
                    if isinstance(profile, dict):
                        profiles[profile.get('symbol')] = profile
                continue

            url = f'{self.BASE_URL}/profile/{symbols_str}'
            data = self._rate_limited_get(url)
            if data:
                self.cache[cache_key] = data
                for profile in data:
                    if isinstance(profile, dict):
                        profiles[profile.get('symbol')] = profile

        return profiles

    def get_historical_prices(
        self, symbol: str, days: int = 250
    ) -> Optional[list[dict]]:
        """Fetch historical daily OHLCV data for a symbol.

        Args:
            symbol: Ticker symbol
            days: Number of trading days to fetch (default: 250)

        Returns:
            List of price dicts (most-recent-first) or None on failure.
        """
        cache_key = f'prices_{symbol}_{days}'
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = f'{self.BASE_URL}/historical-price-full/{symbol}'
        params = {'timeseries': days}
        data = self._rate_limited_get(url, params)
        if data and 'historical' in data:
            result = data['historical']
            self.cache[cache_key] = result
            return result
        return None

    def get_api_stats(self) -> dict:
        """Return API usage statistics."""
        return {
            'cache_entries': len(self.cache),
            'api_calls_made': self.api_calls_made,
            'max_api_calls': self.max_api_calls,
            'rate_limit_reached': self.rate_limit_reached,
        }
