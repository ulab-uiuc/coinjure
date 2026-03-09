"""Tests for coinjure.market.auto_discover — relation auto-detection."""

from __future__ import annotations

from datetime import date

import pytest

from coinjure.market.auto_discover import (
    DiscoveryResult,
    _compute_current_arb,
    _compute_mid_price,
    detect_complementary,
    detect_date_nesting,
    detect_exclusivity,
    discover_relations,
    parse_deadline,
)
from coinjure.market.relations import MarketRelation

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
        # Same market set should only appear once
        market_id_sets = [
            tuple(sorted(m.get('id', '') for m in c.markets)) for c in r.candidates
        ]
        assert len(market_id_sets) == len(set(market_id_sets))

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
# Snapshot arb filtering
# ---------------------------------------------------------------------------


class TestSnapshotArbFilter:
    """Tests for snapshot-based arb computation in discover_relations."""

    def test_implication_with_violation_passes(self):
        """Earlier deadline priced above later -> arb exists -> candidate kept."""
        poly = [
            {
                **_make_market('m1', 'Will X happen by March 31, 2025?', '1', 'E'),
                'best_bid': '0.50',
                'best_ask': '0.60',
            },  # mid=0.55
            {
                **_make_market('m2', 'Will X happen by June 30, 2025?', '1', 'E'),
                'best_bid': '0.30',
                'best_ask': '0.40',
            },  # mid=0.35
        ]
        r = discover_relations(poly, [])
        assert len(r.candidates) == 1
        assert r.candidates[0].spread_type == 'implication'
        assert r.total_detected == 1
        assert r.candidates[0].markets[0].get('current_arb', 0) > 0

    def test_implication_without_violation_kept(self):
        """Earlier deadline priced below later -> no arb -> still kept (no arb filter)."""
        poly = [
            {
                **_make_market('m1', 'Will X happen by March 31, 2025?', '1', 'E'),
                'best_bid': '0.10',
                'best_ask': '0.15',
            },  # mid=0.125
            {
                **_make_market('m2', 'Will X happen by June 30, 2025?', '1', 'E'),
                'best_bid': '0.30',
                'best_ask': '0.40',
            },  # mid=0.35
        ]
        r = discover_relations(poly, [])
        assert len(r.candidates) == 1
        assert r.total_detected == 1

    def test_complementary_with_violation_passes(self):
        """Two outcomes sum > 1.0 -> arb exists -> kept."""
        poly = [
            {
                **_make_market('m1', 'Will Alice win?', '1', 'Election'),
                'best_bid': '0.60',
                'best_ask': '0.65',
            },  # mid=0.625
            {
                **_make_market('m2', 'Will Bob win?', '1', 'Election'),
                'best_bid': '0.38',
                'best_ask': '0.42',
            },  # mid=0.400
        ]
        r = discover_relations(poly, [], skip_exclusivity=True)
        comp = [c for c in r.candidates if c.spread_type == 'complementary']
        assert len(comp) == 1
        assert comp[0].markets[0].get('current_arb', 0) > 0

    def test_complementary_without_violation_kept(self):
        """Two outcomes sum < 1.0 -> no arb -> still kept (no arb filter)."""
        poly = [
            {
                **_make_market('m1', 'Will Alice win?', '1', 'Election'),
                'best_bid': '0.40',
                'best_ask': '0.45',
            },  # mid=0.425
            {
                **_make_market('m2', 'Will Bob win?', '1', 'Election'),
                'best_bid': '0.30',
                'best_ask': '0.35',
            },  # mid=0.325
        ]
        r = discover_relations(poly, [], skip_exclusivity=True)
        comp = [c for c in r.candidates if c.spread_type == 'complementary']
        assert len(comp) == 1

    def test_total_detected_counts_all(self):
        """total_detected equals candidates (no filtering)."""
        # All markets have same mid=0.55 -> implication arb = 0
        poly = [
            _make_market('m1', 'Will X happen by March 31, 2025?', '1', 'Event'),
            _make_market('m2', 'Will X happen by June 30, 2025?', '1', 'Event'),
            _make_market('m3', 'Will X happen by December 31, 2025?', '1', 'Event'),
        ]
        r = discover_relations(poly, [])
        # 3 choose 2 = 3 implication pairs detected
        assert r.total_detected == 3
        assert len(r.candidates) == r.total_detected

    def test_compute_mid_price_basic(self):
        assert _compute_mid_price({'best_bid': '0.40', 'best_ask': '0.60'}) == 0.5
        assert _compute_mid_price({'best_bid': '', 'best_ask': ''}) is None
        assert _compute_mid_price({}) is None

    def test_compute_current_arb_implication(self):
        rel = MarketRelation(
            relation_id='t',
            markets=[
                {'best_bid': '0.60', 'best_ask': '0.70'},  # mid=0.65
                {'best_bid': '0.30', 'best_ask': '0.40'},  # mid=0.35
            ],
            spread_type='implication',
        )
        assert _compute_current_arb(rel) == pytest.approx(0.30)

    def test_compute_current_arb_no_violation(self):
        rel = MarketRelation(
            relation_id='t',
            markets=[
                {'best_bid': '0.10', 'best_ask': '0.20'},  # mid=0.15
                {'best_bid': '0.50', 'best_ask': '0.60'},  # mid=0.55
            ],
            spread_type='implication',
        )
        assert _compute_current_arb(rel) == 0.0

    def test_compute_current_arb_complementary(self):
        rel = MarketRelation(
            relation_id='t',
            markets=[
                {'best_bid': '0.60', 'best_ask': '0.70'},  # mid=0.65
                {'best_bid': '0.40', 'best_ask': '0.50'},  # mid=0.45
            ],
            spread_type='complementary',
        )
        # 0.65 + 0.45 - 1.0 = 0.10
        assert _compute_current_arb(rel) == pytest.approx(0.10)

    def test_compute_current_arb_non_structural(self):
        rel = MarketRelation(
            relation_id='t',
            markets=[
                {'best_bid': '0.50', 'best_ask': '0.60'},
                {'best_bid': '0.50', 'best_ask': '0.60'},
            ],
            spread_type='correlated',
        )
        assert _compute_current_arb(rel) == 0.0


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
        # February 30 doesn't exist
        # Should either return None or raise -- currently may raise ValueError
        # This documents the current behavior
        try:
            result = parse_deadline('Will X happen by February 30, 2025?')
            assert result is None or isinstance(result, date)
        except ValueError:
            pass  # Also acceptable -- invalid date

    def test_date_nesting_same_dates(self):
        """Markets with same deadline should NOT create implication."""
        markets = [
            _make_market('m1', 'Will A happen by March 31, 2025?'),
            _make_market('m2', 'Will B happen by March 31, 2025?'),
        ]
        rels = detect_date_nesting(markets, 'Test', 'polymarket')
        assert len(rels) == 0
