"""Automatic market relation discovery from fetched market data.

Detection layers:
  1. Intra-event date nesting → implication (A ≤ B)
  2. Cross-event verb-based implication (e.g., "called" → "held")
  3. Cross-event temporal correlation (same conflict, different targets)
  4. Intra-event exclusivity (small winner-take-all events)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from coinjure.market.relations import MarketRelation, RelationStore

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
# Theme extraction
# ---------------------------------------------------------------------------

_RE_STRIP_SUFFIX = re.compile(
    r'\s+(?:by|in|before)\s+(?:[\.\?_]+|\d{4}|end\s+of\s+\d{4}|'
    r'\w+\s+\d{1,2}(?:,?\s+\d{4})?)\s*[\?\!]*\s*$',
    re.I,
)
_RE_STRIP_TRAILING = re.compile(r'[\s\?\!\.\:_]+$')
_RE_STRIP_WILL = re.compile(r'^will\s+', re.I)


def extract_theme(event_title: str) -> str:
    """Normalize an event title to a theme key for cross-event matching.

    >>> extract_theme('Ukraine election called by...?')
    'ukraine election called'
    >>> extract_theme('MicroStrategy sells any Bitcoin by ___ ?')
    'microstrategy sells any bitcoin'
    """
    t = event_title.strip()
    # Remove trailing 'by ...' / 'by ___' / 'by ....' style suffixes
    t = re.sub(r'\s+by\s*[\.\?_…]+\s*$', '', t, flags=re.I)
    # Remove structured 'by <date>' suffixes
    t = _RE_STRIP_SUFFIX.sub('', t)
    t = _RE_STRIP_TRAILING.sub('', t)
    t = t.strip().lower()
    t = _RE_STRIP_WILL.sub('', t)
    return t


# ---------------------------------------------------------------------------
# Subject-verb extraction for cross-event implication
# ---------------------------------------------------------------------------

# verb_a -> verb_b means "A verb_a" implies "A verb_b" (prerequisite)
IMPLICATION_VERBS: dict[str, str] = {
    'called': 'held',
    'nominated': 'elected',
    'captures': 'controls',
    'capture': 'control',
    'announced': 'launched',
    'filed': 'approved',
}

_KNOWN_VERBS = re.compile(
    r'\b(called|held|captures?|captured?|invades?|controls?|'
    r'nominated|elected|out|recognizes?|normalizes?|fighting|'
    r'sells?|launched?|announced?|filed|approved|qualif\w+)\b',
    re.I,
)


def extract_subject_verb(theme: str) -> tuple[str, str, str] | None:
    """Extract (subject, verb, rest) from a theme string.

    >>> extract_subject_verb('ukraine election called')
    ('ukraine election', 'called', '')
    >>> extract_subject_verb('russia capture kostyantynivka')
    ('russia', 'capture', 'kostyantynivka')
    """
    m = _KNOWN_VERBS.search(theme)
    if not m:
        return None
    verb = m.group(1).lower()
    subject = theme[:m.start()].strip()
    rest = theme[m.end():].strip()
    if not subject:
        return None
    return (subject, verb, rest)


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
        'token_id': m.get('token_id', ''),
        'no_token_id': m.get('no_token_id', ''),
        'best_bid': m.get('best_bid', m.get('yes_bid', '')),
        'best_ask': m.get('best_ask', m.get('yes_ask', '')),
        'volume': m.get('volume', ''),
        'end_date': m.get('end_date', m.get('close_time', '')),
        'platform': platform,
    }


def _mid(m: dict) -> str:
    return str(m.get('id', m.get('ticker', '')))


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
# Layer 2: Cross-event verb-based implication
# ---------------------------------------------------------------------------


def detect_cross_event_implications(
    theme_groups: dict[str, list[dict]],
    platform: str,
) -> list[MarketRelation]:
    """Detect implication relations across events via verb-based rules.

    Each entry in theme_groups values is:
        {'event_id': str, 'event_title': str, 'markets': list[dict]}
    """
    # Build subject -> [(verb, rest, event_data_list)] index
    subject_idx: dict[str, list[tuple[str, str, list[dict]]]] = {}
    for theme, ev_list in theme_groups.items():
        sv = extract_subject_verb(theme)
        if sv:
            subj, verb, rest = sv
            subject_idx.setdefault(subj, []).append((verb, rest, ev_list))

    relations: list[MarketRelation] = []
    seen: set[str] = set()

    for subj, entries in subject_idx.items():
        for verb_a, rest_a, evs_a in entries:
            implied = IMPLICATION_VERBS.get(verb_a)
            if not implied:
                continue
            for verb_b, rest_b, evs_b in entries:
                if verb_b != implied:
                    continue
                # Pair markets across events by deadline
                for ev_a in evs_a:
                    for m_a in ev_a['markets']:
                        d_a = parse_deadline(m_a.get('question', ''))
                        if d_a is None:
                            continue
                        for ev_b in evs_b:
                            for m_b in ev_b['markets']:
                                d_b = parse_deadline(m_b.get('question', ''))
                                if d_b is None or d_a > d_b:
                                    continue
                                mid_a, mid_b = _mid(m_a), _mid(m_b)
                                pair_key = f'{mid_a}-{mid_b}'
                                if pair_key in seen:
                                    continue
                                seen.add(pair_key)
                                relations.append(MarketRelation(
                                    relation_id=f'x-{mid_a[:8]}-{mid_b[:8]}',
                                    market_a=_enrich(m_a, platform),
                                    market_b=_enrich(m_b, platform),
                                    spread_type='implication',
                                    confidence=0.90,
                                    reasoning=(
                                        f'Cross-event: "{verb_a}" implies "{implied}" '
                                        f'({subj}), {d_a} <= {d_b}'
                                    ),
                                    hypothesis='A <= B',
                                ))
    return relations


# ---------------------------------------------------------------------------
# Layer 3: Cross-event temporal correlation
# ---------------------------------------------------------------------------


def detect_cross_event_correlation(
    theme_groups: dict[str, list[dict]],
    platform: str,
) -> list[MarketRelation]:
    """Detect correlated markets across events with the same verb but different objects."""
    subject_idx: dict[str, list[tuple[str, list[dict]]]] = {}
    for theme, ev_list in theme_groups.items():
        sv = extract_subject_verb(theme)
        if sv:
            subj, verb, obj = sv
            key = f'{subj} {verb}'
            subject_idx.setdefault(key, []).append((obj, ev_list))

    relations: list[MarketRelation] = []
    seen: set[str] = set()

    for key, entries in subject_idx.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                obj_a, evs_a = entries[i]
                obj_b, evs_b = entries[j]
                if obj_a == obj_b:
                    continue
                for ev_a in evs_a:
                    for m_a in ev_a['markets']:
                        d_a = parse_deadline(m_a.get('question', ''))
                        if d_a is None:
                            continue
                        for ev_b in evs_b:
                            for m_b in ev_b['markets']:
                                d_b = parse_deadline(m_b.get('question', ''))
                                if d_b is None or d_a != d_b:
                                    continue
                                mid_a, mid_b = _mid(m_a), _mid(m_b)
                                pair = frozenset([mid_a, mid_b])
                                pk = str(pair)
                                if pk in seen:
                                    continue
                                seen.add(pk)
                                relations.append(MarketRelation(
                                    relation_id=f'c-{mid_a[:8]}-{mid_b[:8]}',
                                    market_a=_enrich(m_a, platform),
                                    market_b=_enrich(m_b, platform),
                                    spread_type='temporal',
                                    confidence=0.60,
                                    reasoning=(
                                        f'Cross-event correlation: {key} '
                                        f'"{obj_a}" <-> "{obj_b}", deadline {d_a}'
                                    ),
                                    hypothesis='correlated',
                                ))
    return relations


# ---------------------------------------------------------------------------
# Layer 4: Intra-event exclusivity (filtered)
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
# Main entry point
# ---------------------------------------------------------------------------


@dataclass
class AutoPairResult:
    """Summary of auto-pair detection run."""
    created: list[MarketRelation] = field(default_factory=list)
    skipped_duplicate: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_layer: dict[str, int] = field(default_factory=dict)


def auto_pair_markets(
    poly_markets: list[dict],
    kalshi_markets: list[dict],
    store: RelationStore,
    skip_exclusivity: bool = False,
    include_correlation: bool = False,
) -> AutoPairResult:
    """Detect and persist market relations from discovered markets.

    Returns a summary of created relations with deduplication against
    existing relations in the store.
    """
    # Existing pairs for dedup (direction-independent)
    existing_pairs: set[frozenset[str]] = set()
    for r in store.list():
        a_id = r.market_a.get('id', '')
        b_id = r.market_b.get('id', '')
        if a_id and b_id:
            existing_pairs.add(frozenset([a_id, b_id]))

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

    # Build theme groups for cross-event layers
    theme_groups: dict[str, list[dict]] = {}
    for eid, mkts in poly_by_event.items():
        title = mkts[0].get('event_title', '')
        theme = extract_theme(title)
        if theme:
            theme_groups.setdefault(theme, []).append(
                {'event_id': eid, 'event_title': title, 'markets': mkts}
            )

    # Layer 2: Cross-event implication
    cross_impl = detect_cross_event_implications(theme_groups, 'polymarket')
    by_layer['cross_event_implication'] = len(cross_impl)
    all_rels.extend(cross_impl)

    # Layer 3: Cross-event correlation (opt-in, lower confidence)
    if include_correlation:
        cross_corr = detect_cross_event_correlation(theme_groups, 'polymarket')
        by_layer['cross_event_correlation'] = len(cross_corr)
        all_rels.extend(cross_corr)

    # Layer 4: Intra-event exclusivity
    if not skip_exclusivity:
        for eid, mkts in poly_by_event.items():
            rels = detect_exclusivity(mkts, mkts[0].get('event_title', ''), 'polymarket')
            by_layer['exclusivity'] = by_layer.get('exclusivity', 0) + len(rels)
            all_rels.extend(rels)

    # --- Deduplicate against existing ---
    created: list[MarketRelation] = []
    skipped = 0
    seen: set[frozenset[str]] = set(existing_pairs)

    for rel in all_rels:
        a_id = rel.market_a.get('id', '')
        b_id = rel.market_b.get('id', '')
        pair_key = frozenset([a_id, b_id])
        if pair_key in seen:
            skipped += 1
            continue
        seen.add(pair_key)
        created.append(rel)

    # Batch save
    if created:
        store.add_batch(created)

    by_type: dict[str, int] = {}
    for r in created:
        by_type[r.spread_type] = by_type.get(r.spread_type, 0) + 1

    return AutoPairResult(
        created=created,
        skipped_duplicate=skipped,
        by_type=by_type,
        by_layer=by_layer,
    )
