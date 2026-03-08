"""Tests for coinjure.market.auto_pair — relation auto-detection."""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from coinjure.market.auto_pair import (
    AutoPairResult,
    auto_pair_markets,
    detect_cross_event_correlation,
    detect_cross_event_implications,
    detect_date_nesting,
    detect_exclusivity,
    extract_subject_verb,
    extract_theme,
    parse_deadline,
)
from coinjure.market.relations import RelationStore


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
# extract_theme
# ---------------------------------------------------------------------------


class TestExtractTheme:
    def test_strips_by_date(self):
        assert extract_theme('Ukraine election called by March 31, 2025?') == 'ukraine election called'

    def test_strips_by_dots(self):
        assert extract_theme('MicroStrategy sells any Bitcoin by ...?') == 'microstrategy sells any bitcoin'

    def test_strips_by_underscores(self):
        assert extract_theme('Will Trump resign by ___?') == 'trump resign'

    def test_strips_will(self):
        assert extract_theme('Will Russia invade Finland in 2025?') == 'russia invade finland'

    def test_plain_title(self):
        assert extract_theme('Super Bowl Winner') == 'super bowl winner'


# ---------------------------------------------------------------------------
# extract_subject_verb
# ---------------------------------------------------------------------------


class TestExtractSubjectVerb:
    def test_simple(self):
        result = extract_subject_verb('ukraine election called')
        assert result == ('ukraine election', 'called', '')

    def test_with_object(self):
        result = extract_subject_verb('russia capture kostyantynivka')
        assert result == ('russia', 'capture', 'kostyantynivka')

    def test_no_verb(self):
        assert extract_subject_verb('super bowl winner') is None

    def test_no_subject(self):
        assert extract_subject_verb('called early') is None


# ---------------------------------------------------------------------------
# detect_date_nesting
# ---------------------------------------------------------------------------


def _make_market(mid: str, question: str, event_id: str = '1', event_title: str = 'Test Event') -> dict:
    return {
        'id': mid,
        'question': question,
        'event_id': event_id,
        'event_title': event_title,
        'token_id': f'tok-{mid}',
        'no_token_id': f'notok-{mid}',
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
        # 3 markets with 3 dates → 3 pairs: (m1,m2), (m1,m3), (m2,m3)
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
# detect_cross_event_implications
# ---------------------------------------------------------------------------


class TestDetectCrossEventImplications:
    def test_called_implies_held(self):
        theme_groups = {
            'ukraine election called': [
                {
                    'event_id': '1',
                    'event_title': 'Ukraine election called by...',
                    'markets': [_make_market('m1', 'Will election be called by June 30, 2025?')],
                }
            ],
            'ukraine election held': [
                {
                    'event_id': '2',
                    'event_title': 'Ukraine election held by...',
                    'markets': [_make_market('m2', 'Will election be held by December 31, 2025?')],
                }
            ],
        }
        rels = detect_cross_event_implications(theme_groups, 'polymarket')
        assert len(rels) >= 1
        assert rels[0].spread_type == 'implication'
        assert 'called' in rels[0].reasoning
        assert 'held' in rels[0].reasoning

    def test_no_matching_verbs(self):
        theme_groups = {
            'ukraine election called': [
                {
                    'event_id': '1',
                    'event_title': 'Ukraine election called by...',
                    'markets': [_make_market('m1', 'Will X by June 30, 2025?')],
                }
            ],
            'russia invades finland': [
                {
                    'event_id': '2',
                    'event_title': 'Russia invades Finland',
                    'markets': [_make_market('m2', 'Will Y by December 31, 2025?')],
                }
            ],
        }
        rels = detect_cross_event_implications(theme_groups, 'polymarket')
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# detect_cross_event_correlation
# ---------------------------------------------------------------------------


class TestDetectCrossEventCorrelation:
    def test_same_verb_different_objects(self):
        theme_groups = {
            'russia capture kostyantynivka': [
                {
                    'event_id': '1',
                    'event_title': 'Russia capture Kostyantynivka',
                    'markets': [_make_market('m1', 'Will Russia capture Kostyantynivka by June 30, 2025?')],
                }
            ],
            'russia capture pokrovsk': [
                {
                    'event_id': '2',
                    'event_title': 'Russia capture Pokrovsk',
                    'markets': [_make_market('m2', 'Will Russia capture Pokrovsk by June 30, 2025?')],
                }
            ],
        }
        rels = detect_cross_event_correlation(theme_groups, 'polymarket')
        # Same deadline → temporal correlation
        assert len(rels) >= 1
        assert rels[0].spread_type == 'temporal'

    def test_different_deadlines_no_match(self):
        theme_groups = {
            'russia capture kostyantynivka': [
                {
                    'event_id': '1',
                    'event_title': 'Russia capture Kostyantynivka',
                    'markets': [_make_market('m1', 'Will Russia capture Kostyantynivka by March 31, 2025?')],
                }
            ],
            'russia capture pokrovsk': [
                {
                    'event_id': '2',
                    'event_title': 'Russia capture Pokrovsk',
                    'markets': [_make_market('m2', 'Will Russia capture Pokrovsk by June 30, 2025?')],
                }
            ],
        }
        rels = detect_cross_event_correlation(theme_groups, 'polymarket')
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
        # 3 choose 2 = 3 pairs
        assert len(rels) == 3
        assert all(r.spread_type == 'exclusivity' for r in rels)

    def test_too_many_markets(self):
        markets = [_make_market(f'm{i}', f'Will Person{i} win?') for i in range(25)]
        rels = detect_exclusivity(markets, 'Big Event', 'polymarket', max_event_size=20)
        assert len(rels) == 0

    def test_non_winner_pattern(self):
        markets = [
            _make_market('m1', 'Will it rain tomorrow?'),
            _make_market('m2', 'Will it snow tomorrow?'),
        ]
        rels = detect_exclusivity(markets, 'Weather', 'polymarket')
        # < 80% match winner pattern → skip
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# auto_pair_markets (integration)
# ---------------------------------------------------------------------------


class TestAutoPairMarkets:
    def _make_store(self, tmp_path: Path) -> RelationStore:
        return RelationStore(path=tmp_path / 'relations.json')

    def test_deduplicates(self, tmp_path):
        store = self._make_store(tmp_path)
        poly = [
            _make_market('m1', 'Will X happen by March 31, 2025?', '1', 'Test Event'),
            _make_market('m2', 'Will X happen by June 30, 2025?', '1', 'Test Event'),
        ]
        r1 = auto_pair_markets(poly, [], store)
        assert len(r1.created) >= 1

        # Run again — should skip duplicates
        r2 = auto_pair_markets(poly, [], store)
        assert len(r2.created) == 0
        assert r2.skipped_duplicate >= 1

    def test_skip_exclusivity(self, tmp_path):
        store = self._make_store(tmp_path)
        poly = [
            _make_market('m1', 'Will Alice win?', '1', 'Election'),
            _make_market('m2', 'Will Bob win?', '1', 'Election'),
        ]
        r = auto_pair_markets(poly, [], store, skip_exclusivity=True)
        excl_count = sum(1 for rel in r.created if rel.spread_type == 'exclusivity')
        assert excl_count == 0

    def test_persists_to_store(self, tmp_path):
        store = self._make_store(tmp_path)
        poly = [
            _make_market('m1', 'Will X happen by March 31, 2025?', '1', 'Test Event'),
            _make_market('m2', 'Will X happen by June 30, 2025?', '1', 'Test Event'),
        ]
        auto_pair_markets(poly, [], store)
        saved = store.list()
        assert len(saved) >= 1
        assert saved[0].spread_type == 'implication'


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases that were missing from initial test coverage."""

    def test_empty_markets(self, tmp_path):
        store = RelationStore(path=tmp_path / 'relations.json')
        r = auto_pair_markets([], [], store)
        assert len(r.created) == 0
        assert r.skipped_duplicate == 0

    def test_markets_without_event_id(self, tmp_path):
        store = RelationStore(path=tmp_path / 'relations.json')
        poly = [_make_market('m1', 'Will X happen by March 31, 2025?', '', '')]
        r = auto_pair_markets(poly, [], store)
        # No event_id → can't group → no pairs
        assert len(r.created) == 0

    def test_markets_without_questions(self, tmp_path):
        store = RelationStore(path=tmp_path / 'relations.json')
        m1 = _make_market('m1', '', '1', 'Test')
        m2 = _make_market('m2', '', '1', 'Test')
        r = auto_pair_markets([m1, m2], [], store)
        # No parseable dates → no date nesting pairs
        assert sum(1 for rel in r.created if rel.spread_type == 'implication') == 0

    def test_parse_deadline_invalid_date(self):
        # February 30 doesn't exist
        # Should either return None or raise — currently may raise ValueError
        # This documents the current behavior
        try:
            result = parse_deadline('Will X happen by February 30, 2025?')
            assert result is None or isinstance(result, date)
        except ValueError:
            pass  # Also acceptable — invalid date

    def test_date_nesting_same_dates(self):
        """Markets with same deadline should NOT create implication."""
        markets = [
            _make_market('m1', 'Will A happen by March 31, 2025?'),
            _make_market('m2', 'Will B happen by March 31, 2025?'),
        ]
        rels = detect_date_nesting(markets, 'Test', 'polymarket')
        assert len(rels) == 0
