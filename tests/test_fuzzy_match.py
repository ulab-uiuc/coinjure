"""Tests for fuzzy_text_match in coinjure.data.fetcher."""

from __future__ import annotations

from coinjure.data.fetcher import fuzzy_text_match


class TestFuzzyTextMatch:
    # --- Exact and substring ---

    def test_exact_word(self):
        assert fuzzy_text_match('trump', 'Will Trump win the election?')

    def test_multi_token_all_present(self):
        assert fuzzy_text_match('trump election', 'Will Trump win the 2024 election?')

    def test_multi_token_one_missing(self):
        assert not fuzzy_text_match('trump bitcoin', 'Will Trump win the election?')

    # --- Prefix matching ---

    def test_prefix_match(self):
        assert fuzzy_text_match('elect', 'Will Trump win the election?')

    def test_prefix_match_short(self):
        assert fuzzy_text_match('fed', 'Federal Reserve rate decision')

    def test_reverse_prefix(self):
        """Text word is prefix of query token."""
        assert fuzzy_text_match(
            'elections', 'elect new president'
        )  # "elect" is prefix of "elections"

    # --- Typo tolerance ---

    def test_typo_one_char(self):
        assert fuzzy_text_match('electon', 'Will Trump win the election?')

    def test_typo_swap(self):
        assert fuzzy_text_match('elcetion', 'Will Trump win the election?')

    def test_too_different(self):
        assert not fuzzy_text_match('banana', 'Will Trump win the election?')

    # --- Case insensitive ---

    def test_case_insensitive(self):
        assert fuzzy_text_match('TRUMP', 'will trump win?')

    # --- Edge cases ---

    def test_empty_query(self):
        assert fuzzy_text_match('', 'anything')

    def test_empty_text(self):
        assert not fuzzy_text_match('trump', '')

    def test_short_tokens_no_fuzzy(self):
        """Tokens < 4 chars only match by prefix, not fuzzy ratio."""
        assert not fuzzy_text_match('xyz', 'abc def ghi')

    def test_numbers(self):
        assert fuzzy_text_match('2024', 'Election results 2024')
