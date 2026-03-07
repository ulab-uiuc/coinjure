"""Unified Market domain object — normalizes Polymarket and Kalshi schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class Market:
    """Platform-agnostic prediction market representation.

    Normalizes the different field names / schemas from Polymarket (Gamma API)
    and Kalshi (REST API) into a single domain object usable by the relation
    discovery, validation, and trading layers.
    """

    market_id: str
    platform: str  # 'polymarket' | 'kalshi'
    question: str
    description: str = ''

    # Pricing
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    last_price: Decimal | None = None
    volume: Decimal | None = None

    # Resolution
    end_date: str | None = None
    resolution_source: str = ''
    status: str = 'active'  # active, closed, resolved

    # Identifiers
    event_id: str = ''
    token_id: str = ''
    no_token_id: str = ''
    ticker_symbol: str = ''
    category: str = ''
    tags: list[str] = field(default_factory=list)

    # Raw platform data (preserved for platform-specific operations)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / Decimal('2')
        return self.last_price

    @classmethod
    def from_polymarket(cls, data: dict[str, Any]) -> Market:
        """Construct from a Polymarket Gamma API market dict."""
        import json

        clob_ids = data.get('clobTokenIds') or data.get('clob_token_ids') or []
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except (json.JSONDecodeError, ValueError):
                clob_ids = []

        token_id = clob_ids[0] if clob_ids else ''
        no_token_id = clob_ids[1] if len(clob_ids) > 1 else ''

        best_bid = data.get('bestBid') or data.get('best_bid')
        best_ask = data.get('bestAsk') or data.get('best_ask')
        last_price = data.get('lastTradePrice') or data.get('outcomePrices')
        volume = data.get('volume')

        return cls(
            market_id=str(data.get('id', data.get('condition_id', ''))),
            platform='polymarket',
            question=data.get('question', ''),
            description=data.get('description', ''),
            best_bid=Decimal(str(best_bid)) if best_bid is not None else None,
            best_ask=Decimal(str(best_ask)) if best_ask is not None else None,
            last_price=Decimal(str(last_price))
            if last_price is not None and not isinstance(last_price, (list, str))
            else None,
            volume=Decimal(str(volume)) if volume is not None else None,
            end_date=data.get('endDate') or data.get('end_date_iso'),
            resolution_source=data.get('resolutionSource', ''),
            status='active' if data.get('active') else 'closed',
            event_id=str(data.get('event_id', '')),
            token_id=token_id,
            no_token_id=no_token_id,
            ticker_symbol=token_id,
            category=data.get('category', ''),
            tags=data.get('tags', []) if isinstance(data.get('tags'), list) else [],
            raw=data,
        )

    @classmethod
    def from_kalshi(cls, data: dict[str, Any]) -> Market:
        """Construct from a Kalshi REST API market dict."""
        yes_bid = data.get('yes_bid')
        yes_ask = data.get('yes_ask')
        last_price = data.get('last_price')
        volume = data.get('volume')

        return cls(
            market_id=data.get('ticker', ''),
            platform='kalshi',
            question=data.get('title', ''),
            description=data.get('subtitle', data.get('rules_primary', '')),
            best_bid=Decimal(str(yes_bid)) / Decimal('100')
            if yes_bid is not None
            else None,
            best_ask=Decimal(str(yes_ask)) / Decimal('100')
            if yes_ask is not None
            else None,
            last_price=Decimal(str(last_price)) / Decimal('100')
            if last_price is not None
            else None,
            volume=Decimal(str(volume)) if volume is not None else None,
            end_date=data.get('close_time') or data.get('expiration_time'),
            resolution_source=data.get('settlement_source_url', ''),
            status=data.get('status', 'active'),
            event_id=data.get('event_ticker', ''),
            ticker_symbol=data.get('ticker', ''),
            category=data.get('category', ''),
            tags=[],
            raw=data,
        )

    def summary(self) -> str:
        """One-line human-readable summary."""
        price = self.mid_price
        price_str = f'{float(price):.2%}' if price is not None else '?'
        return f'[{self.platform}] {self.question[:60]} ({price_str})'
