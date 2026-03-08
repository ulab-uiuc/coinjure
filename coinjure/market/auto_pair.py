"""Automatic market relation discovery from fetched market data.

Detection layers (intra-event only — cross-event/cross-platform left to agent):
  1. Intra-event date nesting → implication (A ≤ B)
  2. Intra-event exclusivity (small winner-take-all events)
  3. Intra-event complementary outcomes (sum ≈ 1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from coinjure.market.relations import MarketRelation

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# Pre-compiled patterns (ordered by specificity)
_RE_MONTH_DAY_YEAR = re.compile(
    r'(?:by|in|before|on)\s+(\w+)\s+(\d{1,2}),?\s+(\d{4})\s*\??$', re.I,
)
_RE_MONTH_DAY = re.compile(
    r'(?:by|in|before|on)\s+(\w+)\s+(\d{1,2})\s*\??$', re.I,
)
_RE_IN_YEAR = re.compile(r'(?:in|by\s+end\s+of)\s+(\d{4})\s*\??$', re.I)
_RE_BEFORE_YEAR = re.compile(r'before\s+(\d{4})\s*\??$', re.I)


def parse_deadline(question: str) -> date | None:
    """Extract the deadline date from a market question.

    Returns None for questions without a date pattern (e.g., sports winners).
    """
    q = question.strip()

    m = _RE_MONTH_DAY_YEAR.search(q)
    if m:
        month_name = m.group(1).lower()
        if month_name in _MONTH_MAP:
            return date(int(m.group(3)), _MONTH_MAP[month_name], int(m.group(2)))

    m = _RE_MONTH_DAY.search(q)
    if m:
        month_name = m.group(1).lower()
        if month_name in _MONTH_MAP:
            return date(date.today().year, _MONTH_MAP[month_name], int(m.group(2)))

    m = _RE_BEFORE_YEAR.search(q)
    if m:
        yr = int(m.group(1))
        if 2020 <= yr <= 2040:
            return date(yr - 1, 12, 31)

    m = _RE_IN_YEAR.search(q)
    if m:
        yr = int(m.group(1))
        if 2020 <= yr <= 2040:
            return date(yr, 12, 31)

    return None


# ---------------------------------------------------------------------------
# Market dict helpers
# ---------------------------------------------------------------------------


def _enrich(m: dict, platform: str) -> dict:
    """Ensure market dict has fields expected by RelationStore."""
    return {
        'id': str(m.get('id', m.get('ticker', ''))),
        'question': m.get('question', m.get('title', '')),
        'event_id': str(m.get('event_id', m.get('event_ticker', ''))),
        'event_title': m.get('event_title', ''),
        'token_ids': m.get('token_ids', []),
        'best_bid': m.get('best_bid', m.get('yes_bid', '')),
        'best_ask': m.get('best_ask', m.get('yes_ask', '')),
        'volume': m.get('volume', ''),
        'end_date': m.get('end_date', m.get('close_time', '')),
        'platform': platform,
    }


def _mid(m: dict) -> str:
    return str(m.get('id', m.get('ticker', '')))


# Types with a structural pricing constraint that can be checked from snapshot
_STRUCTURAL_TYPES = frozenset({'implication', 'exclusivity', 'complementary'})


def _compute_mid_price(m: dict) -> float | None:
    """Compute mid-price from bid/ask already in a market dict."""
    bid = m.get('best_bid', '')
    ask = m.get('best_ask', '')
    try:
        b = float(bid) if bid not in (None, '') else 0.0
        a = float(ask) if ask not in (None, '') else 0.0
    except (ValueError, TypeError):
        return None
    return (b + a) / 2 if (b or a) else None


def _has_liquidity(m: dict) -> bool:
    """Check that a market has non-zero bid AND ask (not a zombie market)."""
    bid = m.get('best_bid', '')
    ask = m.get('best_ask', '')
    try:
        return float(bid) > 0 and float(ask) > 0 if bid and ask else False
    except (ValueError, TypeError):
        return False


def _compute_current_arb(rel: MarketRelation) -> float:
    """Compute current constraint violation from snapshot bid/ask prices.

    Returns 0.0 if no violation, prices unavailable, either leg has no
    liquidity, or non-structural type.
    """
    if not _has_liquidity(rel.market_a) or not _has_liquidity(rel.market_b):
        return 0.0

    mid_a = _compute_mid_price(rel.market_a)
    mid_b = _compute_mid_price(rel.market_b)
    if mid_a is None or mid_b is None:
        return 0.0

    if rel.spread_type == 'implication':
        # A ≤ B: violation when mid_a > mid_b
        return max(mid_a - mid_b, 0.0)
    if rel.spread_type in ('exclusivity', 'complementary'):
        # A + B ≤ 1: violation when sum > 1
        return max(mid_a + mid_b - 1.0, 0.0)
    return 0.0


# ---------------------------------------------------------------------------
# Layer 1: Intra-event date nesting → implication
# ---------------------------------------------------------------------------


def detect_date_nesting(
    markets: list[dict],
    event_title: str,
    platform: str,
) -> list[MarketRelation]:
    """Detect ordered deadline chains within a single event."""
    dated = []
    for m in markets:
        d = parse_deadline(m.get('question', ''))
        if d is not None:
            dated.append((d, m))

    if len(dated) < 2:
        return []

    dated.sort(key=lambda x: x[0])
    relations: list[MarketRelation] = []

    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            d_a, m_a = dated[i]
            d_b, m_b = dated[j]
            if d_a >= d_b:
                continue
            mid_a, mid_b = _mid(m_a), _mid(m_b)
            if not mid_a or not mid_b:
                continue
            relations.append(MarketRelation(
                relation_id=f'{mid_a}-{mid_b}',
                market_a=_enrich(m_a, platform),
                market_b=_enrich(m_b, platform),
                spread_type='implication',
                confidence=0.95,
                reasoning=f'Date nesting: {d_a} <= {d_b} within "{event_title}"',
                hypothesis='A <= B',
            ))

    return relations


# ---------------------------------------------------------------------------
# Layer 2: Intra-event exclusivity (filtered)
# ---------------------------------------------------------------------------

_RE_WINNER = re.compile(r'will\s+.+\s+(win|qualify|be\s+the)', re.I)


def detect_exclusivity(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 20,
) -> list[MarketRelation]:
    """Create exclusivity pairs for small winner-take-all events."""
    if len(markets) > max_event_size:
        return []

    # Only markets with non-zero bids
    active = []
    for m in markets:
        bid = m.get('best_bid') or m.get('yes_bid')
        try:
            if bid and float(bid) > 0:
                active.append(m)
        except (ValueError, TypeError):
            continue
    if len(active) < 2:
        return []

    # Check winner-take-all pattern
    winner_count = sum(1 for m in active if _RE_WINNER.search(m.get('question', '')))
    if winner_count < len(active) * 0.8:
        return []

    relations: list[MarketRelation] = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            m_a, m_b = active[i], active[j]
            mid_a, mid_b = _mid(m_a), _mid(m_b)
            relations.append(MarketRelation(
                relation_id=f'{mid_a}-{mid_b}',
                market_a=_enrich(m_a, platform),
                market_b=_enrich(m_b, platform),
                spread_type='exclusivity',
                confidence=0.99,
                reasoning=f'Mutually exclusive outcomes within "{event_title}"',
                hypothesis='A + B <= 1',
            ))
    return relations


# ---------------------------------------------------------------------------
# Layer 3: Intra-event complementary outcomes (sum ≈ 1)
# ---------------------------------------------------------------------------


def detect_complementary(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 30,
    sum_tolerance: float = 0.30,
) -> list[MarketRelation]:
    """Detect complementary outcomes whose probabilities sum to ~1.

    Unlike exclusivity (which pairs any two mutually exclusive outcomes),
    complementary marks the full partition: all outcomes in the event are
    expected to sum to 1.0.  We only emit pairs when the actual sum of
    mid-prices is within *sum_tolerance* of 1.0.
    """
    if len(markets) < 2 or len(markets) > max_event_size:
        return []

    # Compute mid-prices
    priced: list[tuple[float, dict]] = []
    for m in markets:
        bid = m.get('best_bid') or m.get('yes_bid')
        ask = m.get('best_ask') or m.get('yes_ask')
        try:
            b = float(bid) if bid else 0.0
            a = float(ask) if ask else 0.0
        except (ValueError, TypeError):
            continue
        mid = (b + a) / 2 if (b or a) else 0.0
        if mid > 0:
            priced.append((mid, m))

    if len(priced) < 2:
        return []

    total = sum(p for p, _ in priced)
    if abs(total - 1.0) > sum_tolerance:
        return []

    relations: list[MarketRelation] = []
    for i in range(len(priced)):
        for j in range(i + 1, len(priced)):
            _, m_a = priced[i]
            _, m_b = priced[j]
            mid_a, mid_b = _mid(m_a), _mid(m_b)
            if not mid_a or not mid_b:
                continue
            relations.append(MarketRelation(
                relation_id=f'{mid_a}-{mid_b}',
                market_a=_enrich(m_a, platform),
                market_b=_enrich(m_b, platform),
                spread_type='complementary',
                confidence=0.95,
                reasoning=(
                    f'Complementary outcomes (sum={total:.2f}) '
                    f'within "{event_title}" ({len(priced)} markets)'
                ),
                hypothesis='A + B <= 1',
            ))

    return relations


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@dataclass
class AutoPairResult:
    """Summary of auto-pair detection run."""
    candidates: list[MarketRelation] = field(default_factory=list)
    total_detected: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_layer: dict[str, int] = field(default_factory=dict)


def auto_pair_markets(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
    skip_exclusivity: bool = False,
) -> AutoPairResult:
    """Detect candidate market relations from discovered markets.

    Only performs reliable intra-event structural detection (date nesting,
    exclusivity, complementary). Cross-event and cross-platform relation
    discovery is left to the agent, which has semantic understanding.

    Returns candidates for the agent to review. Does NOT persist anything —
    the agent should run ``market relations add`` for pairs with actual
    opportunities.
    """
    all_rels: list[MarketRelation] = []
    by_layer: dict[str, int] = {}

    # --- Group Polymarket markets by event ---
    poly_by_event: dict[str, list[dict]] = {}
    for m in poly_markets:
        eid = str(m.get('event_id', ''))
        if eid:
            poly_by_event.setdefault(eid, []).append(m)

    # Layer 1: Intra-event date nesting
    for eid, mkts in poly_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_date_nesting(mkts, mkts[0].get('event_title', ''), 'polymarket')
        by_layer['date_nesting'] = by_layer.get('date_nesting', 0) + len(rels)
        all_rels.extend(rels)

    # Layer 2: Intra-event exclusivity
    if not skip_exclusivity:
        for eid, mkts in poly_by_event.items():
            rels = detect_exclusivity(mkts, mkts[0].get('event_title', ''), 'polymarket')
            by_layer['exclusivity'] = by_layer.get('exclusivity', 0) + len(rels)
            all_rels.extend(rels)

    # Layer 3: Intra-event complementary outcomes
    for eid, mkts in poly_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_complementary(mkts, mkts[0].get('event_title', ''), 'polymarket')
        by_layer['complementary'] = by_layer.get('complementary', 0) + len(rels)
        all_rels.extend(rels)

    # --- Deduplicate within this run ---
    deduped: list[MarketRelation] = []
    seen: set[frozenset[str]] = set()

    for rel in all_rels:
        a_id = rel.market_a.get('id', '')
        b_id = rel.market_b.get('id', '')
        pair_key = frozenset([a_id, b_id])
        if pair_key in seen:
            continue
        seen.add(pair_key)
        deduped.append(rel)

    total_detected = len(deduped)

    # --- Snapshot arb filter: keep only structural pairs with current opportunity ---
    candidates: list[MarketRelation] = []
    for rel in deduped:
        arb = _compute_current_arb(rel)
        if arb > 0:
            rel.market_a['current_mid'] = _compute_mid_price(rel.market_a)
            rel.market_b['current_mid'] = _compute_mid_price(rel.market_b)
            rel.market_a['current_arb'] = round(arb, 4)
            candidates.append(rel)

    by_type: dict[str, int] = {}
    for r in candidates:
        by_type[r.spread_type] = by_type.get(r.spread_type, 0) + 1

    return AutoPairResult(
        candidates=candidates,
        total_detected=total_detected,
        by_type=by_type,
        by_layer=by_layer,
    )
