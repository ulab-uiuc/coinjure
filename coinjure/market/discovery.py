"""Market matching utilities for cross-platform market discovery.

Provides a standalone `match_markets` function for use by CLI commands
(market discover) to find equivalent markets across platforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_STOPWORDS = frozenset(
    {'will', 'the', 'a', 'an', 'of', 'in', 'on', 'by', 'to', 'for', 'be', 'is', 'at'}
)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, remove stopwords."""
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return ' '.join(tokens)


@dataclass
class MarketPair:
    """A Polymarket/Kalshi market pair matched by name similarity."""

    poly: dict  # keys: id, question, token_id, best_bid, best_ask, end_date
    kalshi: dict  # keys: ticker, title, yes_bid, yes_ask, close_time
    similarity: float
    already_in_portfolio: bool = False


def match_markets(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
    min_similarity: float = 0.60,
) -> list[MarketPair]:
    """Fuzzy-match Polymarket markets to Kalshi markets by title similarity.

    Returns pairs sorted by descending similarity.  Each Polymarket market
    is matched to at most one Kalshi market (best score wins).
    """
    kalshi_normed = [
        (m, _normalize(m.get('title', ''))) for m in kalshi_markets if m.get('title')
    ]

    pairs: list[MarketPair] = []
    for pm in poly_markets:
        question = pm.get('question', '')
        if not question:
            continue
        pn = _normalize(question)
        best_score = 0.0
        best_km: dict | None = None
        for km, kn in kalshi_normed:
            score = SequenceMatcher(None, pn, kn).ratio()
            if score > best_score:
                best_score = score
                best_km = km

        if best_km is not None and best_score >= min_similarity:
            pairs.append(
                MarketPair(
                    poly=pm,
                    kalshi=best_km,
                    similarity=round(best_score, 3),
                )
            )

    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs
