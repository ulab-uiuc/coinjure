"""Automatic market relation discovery from fetched market data.

Detection layers (intra-event structural only):
  1. Intra-event date nesting → implication (A ≤ B)
  2. Intra-event exclusivity (small winner-take-all events)
  3. Intra-event complementary outcomes (sum ≈ 1)

Cross-event and cross-platform (same_event) relations require semantic
understanding and are left for the LLM agent to discover.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date

from coinjure.market.relations import MarketRelation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

# Pre-compiled patterns (ordered by specificity)
_RE_MONTH_DAY_YEAR = re.compile(
    r'(?:by|in|before|on)\s+(\w+)\s+(\d{1,2}),?\s+(\d{4})\s*\??$',
    re.I,
)
_RE_MONTH_DAY = re.compile(
    r'(?:by|in|before|on)\s+(\w+)\s+(\d{1,2})\s*\??$',
    re.I,
)
_RE_IN_YEAR = re.compile(r'(?:in|by\s+end\s+of)\s+(\d{4})\s*\??$', re.I)
_RE_BEFORE_YEAR = re.compile(r'before\s+(\d{4})\s*\??$', re.I)


def parse_deadline(question: str) -> date | None:
    """Extract the deadline date from a market question.

    Returns None for questions without a date pattern (e.g., sports winners).
    """
    q = question.strip()

    try:
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
    except ValueError:
        return None

    return None


# ---------------------------------------------------------------------------
# Market dict helpers
# ---------------------------------------------------------------------------


def _enrich(m: dict, platform: str) -> dict:
    """Ensure market dict has fields expected by RelationStore."""
    b, a = _bid_ask(m)
    return {
        'id': str(m.get('id', m.get('ticker', ''))),
        'question': _question(m),
        'event_id': str(m.get('event_id', m.get('event_ticker', ''))),
        'event_title': m.get('event_title', ''),
        'token_ids': m.get('token_ids', []),
        'best_bid': b,
        'best_ask': a,
        'volume': m.get('volume', ''),
        'end_date': m.get('end_date', m.get('close_time', '')),
        'platform': platform,
    }


def _mid(m: dict) -> str:
    return str(m.get('id', m.get('ticker', '')))


def _question(m: dict) -> str:
    return m.get('question', '') or m.get('title', '')


def _bid_ask(m: dict) -> tuple[float, float]:
    """Return (bid, ask) normalised to 0-1 range.

    Kalshi prices are in cents (0-100); Polymarket prices are 0-1 decimals.
    """
    bid = m.get('best_bid') or m.get('yes_bid') or 0
    ask = m.get('best_ask') or m.get('yes_ask') or 0
    try:
        b, a = float(bid), float(ask)
    except (ValueError, TypeError):
        return 0.0, 0.0
    # Kalshi cents → decimal
    if b > 1 or a > 1:
        b, a = b / 100, a / 100
    return b, a


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
        d = parse_deadline(_question(m))
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
            relations.append(
                MarketRelation(
                    relation_id=f'{mid_a}-{mid_b}',
                    markets=[_enrich(m_a, platform), _enrich(m_b, platform)],
                    spread_type='implication',
                    confidence=0.95,
                    reasoning=f'Date nesting: {d_a} <= {d_b} within "{event_title}"',
                    hypothesis='A <= B',
                )
            )

    return relations


# ---------------------------------------------------------------------------
# Layer 2: Intra-event exclusivity (filtered)
# ---------------------------------------------------------------------------

_RE_WINNER = re.compile(r'will\s+.+\s+(win|qualify|be\s+the)', re.I)


def detect_exclusivity(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 50,
) -> list[MarketRelation]:
    """Create a single group relation for winner-take-all events."""
    if len(markets) > max_event_size:
        return []

    active = []
    for m in markets:
        b, a = _bid_ask(m)
        if b > 0 and a > 0:
            active.append(m)
    if len(active) < 2:
        return []

    winner_count = sum(1 for m in active if _RE_WINNER.search(_question(m)))
    if winner_count < len(active) * 0.8:
        return []

    market_ids = sorted(_mid(m) for m in active if _mid(m))
    id_part = '-'.join(market_ids[:3]) + (
        f'-+{len(market_ids)-3}' if len(market_ids) > 3 else ''
    )
    relation_id = f'excl-{id_part}'
    return [
        MarketRelation(
            relation_id=relation_id,
            markets=[_enrich(m, platform) for m in active],
            spread_type='exclusivity',
            confidence=0.99,
            reasoning=f'Mutually exclusive outcomes within "{event_title}" ({len(active)} markets)',
            hypothesis='sum(prices) <= 1',
        )
    ]


# ---------------------------------------------------------------------------
# LLM exhaustiveness verification
# ---------------------------------------------------------------------------


def _llm_verify_exhaustive(markets: list[dict], event_title: str) -> bool | None:
    """Ask an LLM whether these markets are mutually exclusive and exhaustive.

    Returns True if verified exhaustive, False if not, None if the LLM is
    unavailable (caller should treat None as unverified and accept the relation).
    """
    api_key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return None

    questions = [m.get('question', '') or m.get('title', '') for m in markets]
    questions_str = '\n'.join(f'- {q}' for q in questions if q)

    prompt = (
        f'Event: "{event_title}"\n'
        f'Markets:\n{questions_str}\n\n'
        'Are these markets MUTUALLY EXCLUSIVE and COLLECTIVELY EXHAUSTIVE?\n'
        'Mutually exclusive = at most one can resolve YES.\n'
        'Collectively exhaustive = at least one MUST resolve YES '
        '(impossible for ALL to resolve NO).\n'
        'Both conditions must hold for valid complementary arbitrage.\n\n'
        'Reply with exactly one word: YES or NO.'
    )

    use_deepseek = bool(os.environ.get('DEEPSEEK_API_KEY'))
    base_url = 'https://api.deepseek.com' if use_deepseek else None
    model = 'deepseek-chat' if use_deepseek else 'gpt-4o-mini'

    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=5,
            temperature=0,
        )
        answer = response.choices[0].message.content.strip().upper()
        verdict = answer.startswith('YES')
        logger.info(
            'LLM exhaustiveness check for "%s": %s → %s',
            event_title, answer, 'PASS' if verdict else 'FAIL',
        )
        return verdict
    except Exception:
        logger.debug('LLM exhaustiveness check failed, accepting relation', exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Layer 3: Intra-event complementary outcomes (sum ≈ 1)
# ---------------------------------------------------------------------------


def detect_complementary(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 50,
    sum_tolerance: float = 0.15,
    llm_verify: bool = True,
) -> list[MarketRelation]:
    """Detect complementary group whose probabilities sum to ~1.

    sum_tolerance of 0.15 means sum must be in [0.85, 1.15].  Markets with
    larger deviations are likely illiquid or not truly exhaustive.

    When llm_verify=True, a lightweight LLM call confirms mutual exclusivity
    and collective exhaustiveness before the relation is accepted.
    """
    if len(markets) < 2 or len(markets) > max_event_size:
        return []

    priced: list[tuple[float, dict]] = []
    for m in markets:
        b, a = _bid_ask(m)
        mid = (b + a) / 2 if (b or a) else 0.0
        if mid > 0:
            priced.append((mid, m))

    if len(priced) < 2:
        return []

    total = sum(p for p, _ in priced)
    if abs(total - 1.0) > sum_tolerance:
        return []

    # LLM verification: confirm markets are truly mutually exclusive and exhaustive.
    # Returns None when LLM is unavailable → accept the relation (don't block).
    llm_note = ''
    if llm_verify:
        result = _llm_verify_exhaustive([m for _, m in priced], event_title)
        if result is False:
            logger.info(
                'Rejecting complementary relation for "%s": LLM says not exhaustive',
                event_title,
            )
            return []
        llm_note = ' [LLM-verified]' if result is True else ' [LLM-unavailable]'

    enriched = [_enrich(m, platform) for _, m in priced]
    market_ids = sorted(_mid(m) for _, m in priced if _mid(m))
    id_part = '-'.join(market_ids[:3]) + (
        f'-+{len(market_ids)-3}' if len(market_ids) > 3 else ''
    )
    relation_id = f'comp-{id_part}'
    return [
        MarketRelation(
            relation_id=relation_id,
            markets=enriched,
            spread_type='complementary',
            confidence=0.95,
            reasoning=(
                f'Complementary outcomes (sum={total:.2f}) '
                f'within "{event_title}" ({len(priced)} markets){llm_note}'
            ),
            hypothesis='sum(prices) = 1',
        )
    ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """Summary of relation discovery run."""

    candidates: list[MarketRelation] = field(default_factory=list)
    total_detected: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_layer: dict[str, int] = field(default_factory=dict)


def discover_relations(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
    skip_exclusivity: bool = False,
) -> DiscoveryResult:
    """Detect candidate market relations from discovered markets.

    Only performs reliable intra-event structural detection (date nesting,
    exclusivity, complementary). Cross-event and cross-platform relation
    discovery is left to the agent, which has semantic understanding.

    Returns candidates for the agent to review. Does NOT persist anything —
    the agent should run ``market relations add`` for relations with actual
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

    # Layer 1: Intra-event date nesting (Polymarket)
    for eid, mkts in poly_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_date_nesting(mkts, mkts[0].get('event_title', ''), 'polymarket')
        by_layer['date_nesting'] = by_layer.get('date_nesting', 0) + len(rels)
        all_rels.extend(rels)

    # Layer 2: Intra-event exclusivity (Polymarket)
    if not skip_exclusivity:
        for eid, mkts in poly_by_event.items():
            rels = detect_exclusivity(
                mkts, mkts[0].get('event_title', ''), 'polymarket'
            )
            by_layer['exclusivity'] = by_layer.get('exclusivity', 0) + len(rels)
            all_rels.extend(rels)

    # Layer 3: Intra-event complementary outcomes (Polymarket)
    for eid, mkts in poly_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_complementary(mkts, mkts[0].get('event_title', ''), 'polymarket')
        by_layer['complementary'] = by_layer.get('complementary', 0) + len(rels)
        all_rels.extend(rels)

    # --- Group Kalshi markets by event ---
    kalshi_by_event: dict[str, list[dict]] = {}
    for m in kalshi_markets:
        eid = str(m.get('event_ticker', ''))
        if eid:
            kalshi_by_event.setdefault(eid, []).append(m)

    for eid, mkts in kalshi_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_date_nesting(mkts, mkts[0].get('title', ''), 'kalshi')
        by_layer['date_nesting'] = by_layer.get('date_nesting', 0) + len(rels)
        all_rels.extend(rels)

    if not skip_exclusivity:
        for eid, mkts in kalshi_by_event.items():
            rels = detect_exclusivity(mkts, mkts[0].get('title', ''), 'kalshi')
            by_layer['exclusivity'] = by_layer.get('exclusivity', 0) + len(rels)
            all_rels.extend(rels)

    for eid, mkts in kalshi_by_event.items():
        if len(mkts) < 2:
            continue
        rels = detect_complementary(mkts, mkts[0].get('title', ''), 'kalshi')
        by_layer['complementary'] = by_layer.get('complementary', 0) + len(rels)
        all_rels.extend(rels)

    # --- Deduplicate within this run ---
    deduped: list[MarketRelation] = []
    seen: set[tuple[str, ...]] = set()

    for rel in all_rels:
        dedup_key = (rel.spread_type, *sorted(m.get('id', '') for m in rel.markets))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        deduped.append(rel)

    total_detected = len(deduped)

    by_type: dict[str, int] = {}
    for r in deduped:
        by_type[r.spread_type] = by_type.get(r.spread_type, 0) + 1

    return DiscoveryResult(
        candidates=deduped,
        total_detected=total_detected,
        by_type=by_type,
        by_layer=by_layer,
    )
