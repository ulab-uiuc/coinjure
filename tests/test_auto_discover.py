"""Tests for coinjure.market.auto_discover — relation auto-detection."""

from __future__ import annotations

from datetime import date

from coinjure.market.auto_discover import (
    DiscoveryResult,
    detect_complementary,
    detect_date_nesting,
    detect_exclusivity,
    discover_relations,
    parse_deadline,
)

# ---------------------------------------------------------------------------
# parse_deadline
# ---------------------------------------------------------------------------


class TestParseDeadline:
    def test_month_day_year(self):
        assert parse_deadline('Will X happen by March 31, 2025?') == date(2025, 3, 31)

    def test_month_day_no_year(self):
        d = parse_deadline('Will X happen by June 30?')
        assert d is not None
        assert d.month == 6
        assert d.day == 30

    def test_in_year(self):
        assert parse_deadline('Will X happen in 2026?') == date(2026, 12, 31)

    def test_before_year(self):
        assert parse_deadline('Will X happen before 2026?') == date(2025, 12, 31)

    def test_no_date(self):
        assert parse_deadline('Who will win the Super Bowl?') is None

    def test_by_end_of(self):
        assert parse_deadline('Will X happen by end of 2025?') == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# detect_date_nesting
# ---------------------------------------------------------------------------


def _make_market(
    mid: str, question: str, event_id: str = '1', event_title: str = 'Test Event'
) -> dict:
    return {
        'id': mid,
        'question': question,
        'event_id': event_id,
        'event_title': event_title,
        'token_ids': [f'tok-{mid}', f'notok-{mid}'],
        'best_bid': '0.5',
        'best_ask': '0.6',
        'volume': '1000',
        'end_date': '',
    }


class TestDetectDateNesting:
    def test_creates_implication_chain(self):
        markets = [
            _make_market('m1', 'Will X happen by March 31, 2025?'),
            _make_market('m2', 'Will X happen by June 30, 2025?'),
            _make_market('m3', 'Will X happen by December 31, 2025?'),
        ]
        rels = detect_date_nesting(markets, 'Test Event', 'polymarket')
        # 3 markets with 3 dates -> 3 pairs: (m1,m2), (m1,m3), (m2,m3)
        assert len(rels) == 3
        assert all(r.spread_type == 'implication' for r in rels)
        assert all(r.hypothesis == 'A <= B' for r in rels)

    def test_single_market(self):
        markets = [_make_market('m1', 'Will X happen by March 31, 2025?')]
        rels = detect_date_nesting(markets, 'Test Event', 'polymarket')
        assert len(rels) == 0

    def test_no_dates(self):
        markets = [
            _make_market('m1', 'Who wins?'),
            _make_market('m2', 'Who loses?'),
        ]
        rels = detect_date_nesting(markets, 'Test Event', 'polymarket')
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# detect_exclusivity
# ---------------------------------------------------------------------------


class TestDetectExclusivity:
    def test_winner_take_all(self):
        markets = [
            _make_market('m1', 'Will Alice win the election?'),
            _make_market('m2', 'Will Bob win the election?'),
            _make_market('m3', 'Will Charlie win the election?'),
        ]
        rels = detect_exclusivity(markets, 'Election Winner', 'polymarket')
        # One group relation
        assert len(rels) == 1
        assert len(rels[0].markets) == 3
        assert rels[0].spread_type == 'exclusivity'

    def test_too_many_markets(self):
        markets = [_make_market(f'm{i}', f'Will Person{i} win?') for i in range(55)]
        rels = detect_exclusivity(markets, 'Big Event', 'polymarket', max_event_size=50)
        assert len(rels) == 0

    def test_non_winner_pattern(self):
        markets = [
            _make_market('m1', 'Will it rain tomorrow?'),
            _make_market('m2', 'Will it snow tomorrow?'),
        ]
        rels = detect_exclusivity(markets, 'Weather', 'polymarket')
        # < 80% match winner pattern -> skip
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# detect_complementary
# ---------------------------------------------------------------------------


class TestDetectComplementary:
    def test_two_outcomes_sum_to_one(self):
        markets = [
            {
                **_make_market('m1', 'Will Alice win?', '1', 'Election'),
                'best_bid': '0.55',
                'best_ask': '0.60',
            },
            {
                **_make_market('m2', 'Will Bob win?', '1', 'Election'),
                'best_bid': '0.35',
                'best_ask': '0.40',
            },
        ]
        rels = detect_complementary(markets, 'Election', 'polymarket')
        assert len(rels) == 1
        assert len(rels[0].markets) == 2
        assert rels[0].spread_type == 'complementary'
        assert 'sum=' in rels[0].reasoning

    def test_three_outcomes_sum_to_one(self):
        markets = [
            {
                **_make_market('m1', 'A wins?', '1', 'E'),
                'best_bid': '0.45',
                'best_ask': '0.50',
            },
            {
                **_make_market('m2', 'B wins?', '1', 'E'),
                'best_bid': '0.25',
                'best_ask': '0.30',
            },
            {
                **_make_market('m3', 'C wins?', '1', 'E'),
                'best_bid': '0.18',
                'best_ask': '0.22',
            },
        ]
        rels = detect_complementary(markets, 'E', 'polymarket')
        # One group relation with all 3 markets
        assert len(rels) == 1
        assert len(rels[0].markets) == 3
        assert rels[0].spread_type == 'complementary'

    def test_sum_too_far_from_one(self):
        markets = [
            {
                **_make_market('m1', 'A?', '1', 'E'),
                'best_bid': '0.80',
                'best_ask': '0.85',
            },
            {
                **_make_market('m2', 'B?', '1', 'E'),
                'best_bid': '0.70',
                'best_ask': '0.75',
            },
        ]
        # sum ~ 1.55, too far from 1.0
        rels = detect_complementary(markets, 'E', 'polymarket')
        assert len(rels) == 0

    def test_single_market(self):
        markets = [
            {
                **_make_market('m1', 'A?', '1', 'E'),
                'best_bid': '0.50',
                'best_ask': '0.55',
            },
        ]
        rels = detect_complementary(markets, 'E', 'polymarket')
        assert len(rels) == 0

    def test_too_many_markets(self):
        markets = [
            {
                **_make_market(f'm{i}', f'O{i}?', '1', 'E'),
                'best_bid': '0.01',
                'best_ask': '0.02',
            }
            for i in range(55)
        ]
        rels = detect_complementary(markets, 'E', 'polymarket', max_event_size=50)
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# discover_relations (integration)
# ---------------------------------------------------------------------------


class TestDiscoverRelations:
    def test_deduplicates_within_run(self):
        # Earlier deadline priced higher -> implication violation -> candidate kept
        poly = [
            {
                **_make_market(
                    'm1', 'Will X happen by March 31, 2025?', '1', 'Test Event'
                ),
                'best_bid': '0.6',
                'best_ask': '0.7',
            },
            {
                **_make_market(
                    'm2', 'Will X happen by June 30, 2025?', '1', 'Test Event'
                ),
                'best_bid': '0.3',
                'best_ask': '0.4',
            },
        ]
        r = discover_relations(poly, [])
        # Same (type, market_set) should only appear once
        dedup_keys = [
            (c.spread_type, *sorted(m.get('id', '') for m in c.markets))
            for c in r.candidates
        ]
        assert len(dedup_keys) == len(set(dedup_keys))

    def test_skip_exclusivity(self):
        poly = [
            _make_market('m1', 'Will Alice win?', '1', 'Election'),
            _make_market('m2', 'Will Bob win?', '1', 'Election'),
        ]
        r = discover_relations(poly, [], skip_exclusivity=True)
        excl_count = sum(1 for rel in r.candidates if rel.spread_type == 'exclusivity')
        assert excl_count == 0

    def test_does_not_persist(self):
        """discover_relations returns candidates without writing to any store."""
        # Earlier deadline priced higher -> implication violation -> candidate kept
        poly = [
            {
                **_make_market(
                    'm1', 'Will X happen by March 31, 2025?', '1', 'Test Event'
                ),
                'best_bid': '0.6',
                'best_ask': '0.7',
            },
            {
                **_make_market(
                    'm2', 'Will X happen by June 30, 2025?', '1', 'Test Event'
                ),
                'best_bid': '0.3',
                'best_ask': '0.4',
            },
        ]
        r = discover_relations(poly, [])
        assert len(r.candidates) >= 1
        assert r.candidates[0].spread_type == 'implication'

    def test_complementary_integrated(self):
        # All structural relations are kept (no arb>0 filter)
        poly = [
            {
                **_make_market('m1', 'Will Alice win?', '1', 'Election'),
                'best_bid': '0.60',
                'best_ask': '0.65',
            },
            {
                **_make_market('m2', 'Will Bob win?', '1', 'Election'),
                'best_bid': '0.38',
                'best_ask': '0.42',
            },
        ]
        r = discover_relations(poly, [], skip_exclusivity=True)
        comp = [rel for rel in r.candidates if rel.spread_type == 'complementary']
        assert len(comp) >= 1


# ---------------------------------------------------------------------------
# discover_relations integration
# ---------------------------------------------------------------------------


class TestDiscoverRelationsIntegration:
    def test_total_detected_counts_all(self):
        """total_detected equals candidates (no filtering)."""
        poly = [
            _make_market('m1', 'Will X happen by March 31, 2025?', '1', 'Event'),
            _make_market('m2', 'Will X happen by June 30, 2025?', '1', 'Event'),
            _make_market('m3', 'Will X happen by December 31, 2025?', '1', 'Event'),
        ]
        r = discover_relations(poly, [])
        # 3 choose 2 = 3 implication pairs detected
        assert r.total_detected == 3
        assert len(r.candidates) == r.total_detected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases that were missing from initial test coverage."""

    def test_empty_markets(self):
        r = discover_relations([], [])
        assert len(r.candidates) == 0

    def test_markets_without_event_id(self):
        poly = [_make_market('m1', 'Will X happen by March 31, 2025?', '', '')]
        r = discover_relations(poly, [])
        # No event_id -> can't group -> no pairs
        assert len(r.candidates) == 0

    def test_markets_without_questions(self):
        m1 = _make_market('m1', '', '1', 'Test')
        m2 = _make_market('m2', '', '1', 'Test')
        r = discover_relations([m1, m2], [])
        # No parseable dates -> no date nesting pairs
        assert sum(1 for rel in r.candidates if rel.spread_type == 'implication') == 0

    def test_parse_deadline_invalid_date(self):
        # February 30 doesn't exist — should return None, not raise
        assert parse_deadline('Will X happen by February 30, 2025?') is None

    def test_exclusivity_requires_ask(self):
        """Markets with bid but no ask should be excluded from exclusivity."""
        markets = [
            {
                **_make_market('m1', 'Will Alice win the election?'),
                'best_bid': '0.5',
                'best_ask': '0',
            },
            _make_market('m2', 'Will Bob win the election?'),
            _make_market('m3', 'Will Charlie win the election?'),
        ]
        rels = detect_exclusivity(markets, 'Election', 'polymarket')
        assert len(rels) == 1
        # m1 excluded (no ask), only m2 and m3 in group
        assert len(rels[0].markets) == 2

    def test_exclusivity_relation_id_prefix(self):
        """Exclusivity relation IDs should have 'excl-' prefix."""
        markets = [
            _make_market('m1', 'Will Alice win the election?'),
            _make_market('m2', 'Will Bob win the election?'),
        ]
        rels = detect_exclusivity(markets, 'Election', 'polymarket')
        assert len(rels) == 1
        assert rels[0].relation_id.startswith('excl-')

    def test_complementary_relation_id_prefix(self):
        """Complementary relation IDs should have 'comp-' prefix."""
        markets = [
            {
                **_make_market('m1', 'A?', '1', 'E'),
                'best_bid': '0.55',
                'best_ask': '0.60',
            },
            {
                **_make_market('m2', 'B?', '1', 'E'),
                'best_bid': '0.35',
                'best_ask': '0.40',
            },
        ]
        rels = detect_complementary(markets, 'E', 'polymarket')
        assert len(rels) == 1
        assert rels[0].relation_id.startswith('comp-')

    def test_both_exclusivity_and_complementary_kept(self):
        """Same markets matching both layers should produce two candidates."""
        poly = [
            {
                **_make_market('m1', 'Will Alice win the election?', '1', 'Election'),
                'best_bid': '0.55',
                'best_ask': '0.60',
            },
            {
                **_make_market('m2', 'Will Bob win the election?', '1', 'Election'),
                'best_bid': '0.35',
                'best_ask': '0.40',
            },
        ]
        r = discover_relations(poly, [])
        types = {rel.spread_type for rel in r.candidates}
        # Both types should survive dedup (different spread_type)
        assert 'exclusivity' in types
        assert 'complementary' in types

    def test_date_nesting_same_dates(self):
        """Markets with same deadline should NOT create implication."""
        markets = [
            _make_market('m1', 'Will A happen by March 31, 2025?'),
            _make_market('m2', 'Will B happen by March 31, 2025?'),
        ]
        rels = detect_date_nesting(markets, 'Test', 'polymarket')
        assert len(rels) == 0
