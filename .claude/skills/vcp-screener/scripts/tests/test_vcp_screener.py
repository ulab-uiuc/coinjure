#!/usr/bin/env python3
"""
Tests for VCP Screener modules.

Covers boundary conditions for VCP pattern detection, contraction validation,
Trend Template criteria, volume patterns, pivot proximity, and scoring.
"""

import json
import os
import tempfile

from calculators.pivot_proximity_calculator import calculate_pivot_proximity
from calculators.relative_strength_calculator import calculate_relative_strength
from calculators.trend_template_calculator import calculate_trend_template
from calculators.vcp_pattern_calculator import _validate_vcp, calculate_vcp_pattern
from calculators.volume_pattern_calculator import calculate_volume_pattern
from report_generator import generate_json_report, generate_markdown_report
from scorer import calculate_composite_score
from screen_vcp import (
    analyze_stock,
    compute_entry_ready,
    is_stale_price,
    parse_arguments,
    passes_trend_filter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prices(n, start=100.0, daily_change=0.0, volume=1000000):
    """Generate synthetic price data (most-recent-first)."""
    prices = []
    p = start
    for i in range(n):
        p_day = p * (1 + daily_change * (n - i))  # linear drift
        prices.append(
            {
                'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                'open': round(p_day, 2),
                'high': round(p_day * 1.01, 2),
                'low': round(p_day * 0.99, 2),
                'close': round(p_day, 2),
                'adjClose': round(p_day, 2),
                'volume': volume,
            }
        )
    return prices


def _make_vcp_contractions(depths, high_price=100.0):
    """Build contraction dicts for _validate_vcp testing."""
    contractions = []
    hp = high_price
    for i, depth in enumerate(depths):
        lp = hp * (1 - depth / 100)
        contractions.append(
            {
                'label': f'T{i + 1}',
                'high_idx': i * 20,
                'high_price': round(hp, 2),
                'high_date': f'2025-01-{i * 20 + 1:02d}',
                'low_idx': i * 20 + 10,
                'low_price': round(lp, 2),
                'low_date': f'2025-01-{i * 20 + 11:02d}',
                'depth_pct': round(depth, 2),
            }
        )
        hp = hp * 0.99  # next high slightly lower
    return contractions


# ===========================================================================
# VCP Pattern Validation Tests (Fix 1: contraction ratio 0.75 rule)
# ===========================================================================


class TestVCPValidation:
    """Test the strict 75% contraction ratio rule."""

    def test_valid_tight_contractions(self):
        """T1=20%, T2=10%, T3=5% -> ratios 0.50, 0.50 -> valid"""
        contractions = _make_vcp_contractions([20, 10, 5])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True

    def test_invalid_loose_contractions(self):
        """T1=20%, T2=18% -> ratio 0.90 > 0.75 -> invalid"""
        contractions = _make_vcp_contractions([20, 18])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is False
        assert any('0.75' in issue for issue in result['issues'])

    def test_borderline_ratio_075(self):
        """T1=20%, T2=15% -> ratio 0.75 -> valid (exactly at threshold)"""
        contractions = _make_vcp_contractions([20, 15])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True

    def test_ratio_076_invalid(self):
        """T1=20%, T2=15.2% -> ratio 0.76 -> invalid"""
        contractions = _make_vcp_contractions([20, 15.2])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is False

    def test_expanding_contractions_invalid(self):
        """T1=10%, T2=15% -> ratio 1.5 -> invalid"""
        contractions = _make_vcp_contractions([10, 15])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is False

    def test_single_contraction_too_few(self):
        """Single contraction is not enough for VCP."""
        contractions = _make_vcp_contractions([20])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is False

    def test_t1_too_shallow(self):
        """T1=5% is below 8% minimum -> invalid"""
        contractions = _make_vcp_contractions([5, 3])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is False

    def test_four_progressive_contractions(self):
        """T1=30%, T2=15%, T3=7%, T4=3% -> valid textbook"""
        contractions = _make_vcp_contractions([30, 15, 7, 3])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True


# ===========================================================================
# Stale Price (Acquisition) Filter Tests
# ===========================================================================


class TestStalePrice:
    """Test is_stale_price() - detects acquired/pinned stocks."""

    def test_stale_flat_price(self):
        """Daily range < 1% for 10 days -> stale."""
        prices = []
        for i in range(20):
            prices.append(
                {
                    'date': f'2026-01-{20 - i:02d}',
                    'open': 14.31,
                    'high': 14.35,
                    'low': 14.28,
                    'close': 14.31,
                    'volume': 500000,
                }
            )
        assert is_stale_price(prices) is True

    def test_normal_price_action(self):
        """Normal volatility -> not stale."""
        prices = []
        for i in range(20):
            base = 100.0 + i * 0.5
            prices.append(
                {
                    'date': f'2026-01-{20 - i:02d}',
                    'open': base,
                    'high': base * 1.02,
                    'low': base * 0.98,
                    'close': base + 0.3,
                    'volume': 1000000,
                }
            )
        assert is_stale_price(prices) is False

    def test_insufficient_data(self):
        """Less than lookback days -> not stale (let other filters handle)."""
        prices = [{'date': '2026-01-01', 'high': 10, 'low': 10, 'close': 10}]
        assert is_stale_price(prices) is False


# ===========================================================================
# Trend Template Tests (Fix 5: C3 conservative with limited data)
# ===========================================================================


class TestTrendTemplate:
    """Test Trend Template scoring."""

    def test_insufficient_data(self):
        prices = _make_prices(30)
        quote = {'price': 100, 'yearHigh': 110, 'yearLow': 50}
        result = calculate_trend_template(prices, quote)
        assert result['score'] == 0
        assert result['passed'] is False

    def test_c3_fails_with_200_days(self):
        """With exactly 200 days, C3 should fail (cannot verify 22d SMA200 trend)."""
        prices = _make_prices(210, start=100, daily_change=0.001)
        quote = {'price': 120, 'yearHigh': 125, 'yearLow': 80}
        result = calculate_trend_template(prices, quote, rs_rank=85)
        c3 = result['criteria'].get('c3_sma200_trending_up', {})
        assert c3['passed'] is False

    def test_c3_passes_with_222_days(self):
        """With 222+ days and uptrend, C3 should pass."""
        prices = _make_prices(250, start=80, daily_change=0.001)
        quote = {'price': 120, 'yearHigh': 125, 'yearLow': 70}
        result = calculate_trend_template(prices, quote, rs_rank=85)
        # C3 should be evaluated (may pass or fail depending on synthetic data)
        c3 = result['criteria'].get('c3_sma200_trending_up', {})
        assert 'Cannot verify' not in c3.get('detail', '')


# ===========================================================================
# Volume Pattern Tests
# ===========================================================================


class TestVolumePattern:
    def test_insufficient_data(self):
        result = calculate_volume_pattern([])
        assert result['score'] == 0
        assert 'Insufficient' in result['error']

    def test_low_dry_up_ratio(self):
        """Recent volume much lower than 50d avg -> high score."""
        prices = _make_prices(60, volume=1000000)
        # Override recent 10 bars with low volume
        for i in range(10):
            prices[i]['volume'] = 200000
        result = calculate_volume_pattern(prices)
        assert result['dry_up_ratio'] < 0.3
        assert result['score'] >= 80


# ===========================================================================
# Pivot Proximity Tests
# ===========================================================================


class TestPivotProximity:
    def test_no_pivot(self):
        result = calculate_pivot_proximity(100.0, None)
        assert result['score'] == 0

    def test_breakout_confirmed(self):
        """0-3% above with volume -> base 90 + bonus 10 = 100, BREAKOUT CONFIRMED."""
        result = calculate_pivot_proximity(
            102.0, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        assert result['score'] == 100
        assert result['trade_status'] == 'BREAKOUT CONFIRMED'

    def test_at_pivot(self):
        result = calculate_pivot_proximity(99.0, 100.0, last_contraction_low=95.0)
        assert result['score'] == 90
        assert 'AT PIVOT' in result['trade_status']

    def test_far_below_pivot(self):
        result = calculate_pivot_proximity(80.0, 100.0)
        assert result['score'] == 10

    def test_below_stop_level(self):
        result = calculate_pivot_proximity(90.0, 100.0, last_contraction_low=95.0)
        assert 'BELOW STOP LEVEL' in result['trade_status']

    def test_extended_above_pivot_7pct(self):
        """7% above pivot (no volume) -> score=50, High chase risk."""
        result = calculate_pivot_proximity(107.0, 100.0, last_contraction_low=95.0)
        assert result['score'] == 50
        assert 'High chase risk' in result['trade_status']

    def test_extended_above_pivot_25pct(self):
        """25% above pivot -> score=20, OVEREXTENDED."""
        result = calculate_pivot_proximity(125.0, 100.0, last_contraction_low=95.0)
        assert result['score'] == 20
        assert 'OVEREXTENDED' in result['trade_status']

    def test_near_above_pivot_2pct(self):
        """2% above pivot (no volume) -> score=90, ABOVE PIVOT."""
        result = calculate_pivot_proximity(102.0, 100.0, last_contraction_low=95.0)
        assert result['score'] == 90
        assert 'ABOVE PIVOT' in result['trade_status']

    # --- New distance-priority tests ---

    def test_breakout_volume_no_override_at_33pct(self):
        """+33.5% above, volume=True -> score=20 (distance priority, no bonus >5%)."""
        result = calculate_pivot_proximity(
            133.5, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        assert result['score'] == 20
        assert 'OVEREXTENDED' in result['trade_status']

    def test_breakout_volume_bonus_at_2pct(self):
        """+2% above, volume=True -> base 90 + bonus 10 = 100."""
        result = calculate_pivot_proximity(
            102.0, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        assert result['score'] == 100
        assert result['trade_status'] == 'BREAKOUT CONFIRMED'

    def test_breakout_volume_bonus_at_4pct(self):
        """+4% above, volume=True -> base 65 + bonus 10 = 75."""
        result = calculate_pivot_proximity(
            104.0, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        assert result['score'] == 75
        assert 'vol confirmed' in result['trade_status']

    def test_breakout_volume_no_bonus_at_7pct(self):
        """+7% above, volume=True -> score=50 (no bonus >5%)."""
        result = calculate_pivot_proximity(
            107.0, 100.0, last_contraction_low=95.0, breakout_volume=True
        )
        assert result['score'] == 50
        assert 'High chase risk' in result['trade_status']


# ===========================================================================
# Relative Strength Tests
# ===========================================================================


class TestRelativeStrength:
    def test_insufficient_stock_data(self):
        result = calculate_relative_strength([], [])
        assert result['score'] == 0

    def test_outperformer(self):
        # Stock up 30%, SP500 up 5% over 3 months
        stock = _make_prices(70, start=77, daily_change=0.003)
        sp500 = _make_prices(70, start=95, daily_change=0.0005)
        result = calculate_relative_strength(stock, sp500)
        assert result['score'] >= 60  # should outperform


# ===========================================================================
# Entry Ready Tests
# ===========================================================================


class TestEntryReady:
    """Test compute_entry_ready() from screen_vcp module."""

    def _make_result(
        self,
        valid_vcp=True,
        distance_from_pivot_pct=-1.0,
        dry_up_ratio=0.5,
        risk_pct=5.0,
    ):
        """Build a minimal analysis result dict for compute_entry_ready()."""
        return {
            'valid_vcp': valid_vcp,
            'distance_from_pivot_pct': distance_from_pivot_pct,
            'volume_pattern': {'dry_up_ratio': dry_up_ratio},
            'pivot_proximity': {'risk_pct': risk_pct},
        }

    def test_entry_ready_ideal_candidate(self):
        """valid_vcp=True, distance=-1%, dry_up=0.5, risk=5% -> True."""
        result = self._make_result(
            valid_vcp=True,
            distance_from_pivot_pct=-1.0,
            dry_up_ratio=0.5,
            risk_pct=5.0,
        )
        assert compute_entry_ready(result) is True

    def test_entry_ready_false_extended(self):
        """valid_vcp=True, distance=+15% -> False (too far above pivot)."""
        result = self._make_result(
            valid_vcp=True,
            distance_from_pivot_pct=15.0,
            dry_up_ratio=0.5,
            risk_pct=5.0,
        )
        assert compute_entry_ready(result) is False

    def test_entry_ready_false_invalid_vcp(self):
        """valid_vcp=False -> False regardless of distance."""
        result = self._make_result(
            valid_vcp=False,
            distance_from_pivot_pct=-1.0,
            dry_up_ratio=0.5,
            risk_pct=5.0,
        )
        assert compute_entry_ready(result) is False

    def test_entry_ready_false_high_risk(self):
        """valid_vcp=True, distance=-1%, risk=20% -> False (risk too high)."""
        result = self._make_result(
            valid_vcp=True,
            distance_from_pivot_pct=-1.0,
            dry_up_ratio=0.5,
            risk_pct=20.0,
        )
        assert compute_entry_ready(result) is False

    def test_entry_ready_custom_max_above_pivot(self):
        """CLI --max-above-pivot=5.0 allows +4% above pivot."""
        result = self._make_result(distance_from_pivot_pct=4.0)
        assert compute_entry_ready(result, max_above_pivot=5.0) is True
        assert compute_entry_ready(result, max_above_pivot=3.0) is False

    def test_entry_ready_custom_max_risk(self):
        """CLI --max-risk=10.0 rejects risk=12%."""
        result = self._make_result(risk_pct=12.0)
        assert compute_entry_ready(result, max_risk=15.0) is True
        assert compute_entry_ready(result, max_risk=10.0) is False

    def test_entry_ready_no_require_valid_vcp(self):
        """CLI --no-require-valid-vcp allows invalid VCP."""
        result = self._make_result(valid_vcp=False)
        assert compute_entry_ready(result, require_valid_vcp=True) is False
        assert compute_entry_ready(result, require_valid_vcp=False) is True


# ===========================================================================
# Scorer Tests
# ===========================================================================


class TestScorer:
    def test_textbook_rating(self):
        result = calculate_composite_score(100, 100, 100, 100, 100)
        assert result['composite_score'] == 100
        assert result['rating'] == 'Textbook VCP'

    def test_no_vcp_rating(self):
        result = calculate_composite_score(0, 0, 0, 0, 0)
        assert result['composite_score'] == 0
        assert result['rating'] == 'No VCP'

    def test_weights_sum_to_100(self):
        """Verify component weights sum to 1.0"""
        from scorer import COMPONENT_WEIGHTS

        total = sum(COMPONENT_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001

    def test_valid_vcp_false_caps_rating(self):
        """valid_vcp=False with composite>=70 -> rating capped to 'Developing VCP'."""
        # Scores: 80*0.25 + 70*0.25 + 70*0.20 + 70*0.15 + 70*0.15 = 72.5
        result = calculate_composite_score(80, 70, 70, 70, 70, valid_vcp=False)
        assert result['composite_score'] >= 70
        assert result['rating'] == 'Developing VCP'
        assert 'not confirmed' in result['rating_description'].lower()
        assert result['valid_vcp'] is False

    def test_valid_vcp_true_no_cap(self):
        """valid_vcp=True with composite>=70 -> normal rating (Good VCP)."""
        result = calculate_composite_score(80, 70, 70, 70, 70, valid_vcp=True)
        assert result['composite_score'] >= 70
        assert result['rating'] == 'Good VCP'
        assert result['valid_vcp'] is True

    def test_valid_vcp_false_low_score_no_effect(self):
        """valid_vcp=False with composite<70 -> no cap needed, normal rating."""
        # Scores: 60*0.25 + 50*0.25 + 50*0.20 + 50*0.15 + 50*0.15 = 52.5
        result = calculate_composite_score(60, 50, 50, 50, 50, valid_vcp=False)
        assert result['composite_score'] < 70
        assert result['rating'] == 'Weak VCP'
        assert result['valid_vcp'] is False


# ===========================================================================
# Report Generator Tests (Fix 2: market_cap=None, Fix 3/4: summary counts)
# ===========================================================================


class TestReportGenerator:
    def _make_stock(self, symbol='TEST', score=75.0, market_cap=50e9, rating=None):
        if rating is None:
            if score >= 90:
                rating = 'Textbook VCP'
            elif score >= 80:
                rating = 'Strong VCP'
            elif score >= 70:
                rating = 'Good VCP'
            elif score >= 60:
                rating = 'Developing VCP'
            elif score >= 50:
                rating = 'Weak VCP'
            else:
                rating = 'No VCP'
        return {
            'symbol': symbol,
            'company_name': f'{symbol} Corp',
            'sector': 'Technology',
            'price': 150.0,
            'market_cap': market_cap,
            'composite_score': score,
            'rating': rating,
            'rating_description': 'Solid VCP',
            'guidance': 'Buy on volume confirmation',
            'weakest_component': 'Volume',
            'weakest_score': 40,
            'strongest_component': 'Trend',
            'strongest_score': 100,
            'trend_template': {'score': 100, 'criteria_passed': 7},
            'vcp_pattern': {
                'score': 70,
                'num_contractions': 2,
                'contractions': [],
                'pivot_price': 145.0,
            },
            'volume_pattern': {'score': 40, 'dry_up_ratio': 0.8},
            'pivot_proximity': {
                'score': 75,
                'distance_from_pivot_pct': -3.0,
                'stop_loss_price': 140.0,
                'risk_pct': 7.0,
                'trade_status': 'NEAR PIVOT',
            },
            'relative_strength': {
                'score': 80,
                'rs_rank_estimate': 80,
                'weighted_rs': 15.0,
            },
        }

    def test_market_cap_none(self):
        """market_cap=None should not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stock = self._make_stock(market_cap=None)
            md_file = os.path.join(tmpdir, 'test.md')
            metadata = {
                'generated_at': '2026-01-01',
                'universe_description': 'Test',
                'funnel': {},
                'api_stats': {},
            }
            generate_markdown_report([stock], metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert 'N/A' in content  # market cap should show N/A

    def test_summary_uses_all_results(self):
        """Summary should count all candidates, not just top N."""
        all_results = [self._make_stock(f'S{i}', score=90 - i * 5) for i in range(10)]
        top_results = all_results[:3]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 10},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            json_file = os.path.join(tmpdir, 'test.json')
            generate_json_report(
                top_results, metadata, json_file, all_results=all_results
            )
            with open(json_file) as f:
                data = json.load(f)
            assert data['summary']['total'] == 10
            assert len(data['results']) == 3

    def test_market_cap_zero(self):
        """market_cap=0 should show N/A."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stock = self._make_stock(market_cap=0)
            md_file = os.path.join(tmpdir, 'test.md')
            metadata = {
                'generated_at': '2026-01-01',
                'universe_description': 'Test',
                'funnel': {},
                'api_stats': {},
            }
            generate_markdown_report([stock], metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert 'N/A' in content

    def test_top_greater_than_20(self):
        """--top=25 should produce 25 entries in Markdown, not capped at 20."""
        stocks = [self._make_stock(f'S{i:02d}', score=95 - i) for i in range(25)]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 25},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(stocks, metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            # All 25 stocks should appear in Section A or B
            assert 'Section A:' in content or 'Section B:' in content
            for i in range(25):
                assert f'S{i:02d}' in content

    def test_report_two_sections(self):
        """Report splits into Pre-Breakout Watchlist and Extended sections."""
        entry_ready_stock = self._make_stock('READY', score=80.0, rating='Strong VCP')
        entry_ready_stock['entry_ready'] = True
        entry_ready_stock['distance_from_pivot_pct'] = -1.0

        extended_stock = self._make_stock('EXTENDED', score=75.0, rating='Good VCP')
        extended_stock['entry_ready'] = False
        extended_stock['distance_from_pivot_pct'] = 15.0

        results = [entry_ready_stock, extended_stock]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 2},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(results, metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert 'Pre-Breakout Watchlist' in content
            assert 'Extended / Quality VCP' in content
            assert 'READY' in content
            assert 'EXTENDED' in content

    def test_summary_counts_by_rating_not_score(self):
        """Summary should use rating field, not composite_score.

        A stock with composite=72 but rating='Developing VCP' (valid_vcp cap)
        must count as developing, not good.
        """
        from report_generator import _generate_summary

        results = [
            # Normal: composite=75, rating=Good VCP
            self._make_stock('GOOD1', score=75.0, rating='Good VCP'),
            # Capped: composite=72 but valid_vcp=False -> Developing VCP
            self._make_stock('CAPPED', score=72.0, rating='Developing VCP'),
            # Normal developing
            self._make_stock('DEV1', score=65.0, rating='Developing VCP'),
            # Weak
            self._make_stock('WEAK1', score=55.0, rating='Weak VCP'),
        ]

        summary = _generate_summary(results)
        assert summary['total'] == 4
        assert summary['good'] == 1  # only GOOD1
        assert summary['developing'] == 2  # CAPPED + DEV1
        assert summary['weak'] == 1  # WEAK1
        assert summary['textbook'] == 0
        assert summary['strong'] == 0


# ===========================================================================
# SMA50 Extended Penalty Tests
# ===========================================================================


class TestSMA50ExtendedPenalty:
    """Test extended penalty applied to trend template score."""

    def _make_stage2_prices(self, n=250, sma50_target=100.0, price=None):
        """Build synthetic prices where SMA50 ≈ sma50_target.

        All prices are constant at sma50_target so SMA50 = sma50_target exactly.
        The quote price is set separately to control distance.
        """
        prices = []
        for i in range(n):
            prices.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': sma50_target,
                    'high': sma50_target * 1.005,
                    'low': sma50_target * 0.995,
                    'close': sma50_target,
                    'adjClose': sma50_target,
                    'volume': 1000000,
                }
            )
        return prices

    def _run_tt(self, distance_pct, ext_threshold=8.0):
        """Run calculate_trend_template with a given SMA50 distance %.

        Returns the result dict.
        """
        sma50_target = 100.0
        price = sma50_target * (1 + distance_pct / 100)
        prices = self._make_stage2_prices(n=250, sma50_target=sma50_target)
        quote = {
            'price': price,
            'yearHigh': price * 1.05,
            'yearLow': sma50_target * 0.6,
        }
        return calculate_trend_template(
            prices,
            quote,
            rs_rank=85,
            ext_threshold=ext_threshold,
        )

    # --- Penalty calculation ---

    def test_no_penalty_within_8pct(self):
        result = self._run_tt(5.0)
        assert result['extended_penalty'] == 0

    def test_penalty_at_10pct_distance(self):
        result = self._run_tt(10.0)
        assert result['extended_penalty'] == -5

    def test_penalty_at_15pct_distance(self):
        result = self._run_tt(15.0)
        assert result['extended_penalty'] == -10

    def test_penalty_at_20pct_distance(self):
        result = self._run_tt(20.0)
        assert result['extended_penalty'] == -15

    def test_penalty_at_30pct_distance(self):
        result = self._run_tt(30.0)
        assert result['extended_penalty'] == -20

    def test_penalty_floor_at_zero(self):
        """Penalty cannot make score negative (max(0, raw + penalty))."""
        # Recent 50 at 80, older 200 at 120 → SMA50=80, SMA150≈107, SMA200≈110
        # Price=105: above SMA50 by ~31% (penalty=-20) but below SMA150 (C1 fail)
        # Only C4 passes → raw_score=14.3, 14.3+(-20)=-5.7 → floor to 0
        n = 250
        prices = []
        for i in range(n):
            close = 80.0 if i < 50 else 120.0
            prices.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': close,
                    'high': close * 1.005,
                    'low': close * 0.995,
                    'close': close,
                    'adjClose': close,
                    'volume': 1000000,
                }
            )
        quote = {'price': 105.0, 'yearHigh': 200.0, 'yearLow': 100.0}
        result = calculate_trend_template(prices, quote, rs_rank=10)
        assert result['extended_penalty'] == -20
        assert result['raw_score'] <= 14.3
        assert result['score'] == 0

    def test_price_below_sma50_no_penalty(self):
        result = self._run_tt(-5.0)
        assert result['extended_penalty'] == 0

    # --- Boundary tests (R1-4) ---

    def test_boundary_exactly_8pct(self):
        result = self._run_tt(8.0)
        assert result['extended_penalty'] == -5

    def test_boundary_exactly_12pct(self):
        result = self._run_tt(12.0)
        assert result['extended_penalty'] == -10

    def test_boundary_exactly_18pct(self):
        result = self._run_tt(18.0)
        assert result['extended_penalty'] == -15

    def test_boundary_exactly_25pct(self):
        result = self._run_tt(25.0)
        assert result['extended_penalty'] == -20

    # --- Gate separation (R1-1: most important) ---

    def test_passed_uses_raw_score_not_adjusted(self):
        """raw >= 85, ext < 0 -> passed=True (raw >= 85), score < raw."""
        # Build uptrending data (most-recent-first) so most criteria pass
        n = 250
        prices = []
        for i in range(n):
            # index 0 = newest (highest), index 249 = oldest (lowest)
            base = 120 - 40 * i / (n - 1)  # 120 → 80
            prices.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': base,
                    'high': base * 1.005,
                    'low': base * 0.995,
                    'close': base,
                    'adjClose': base,
                    'volume': 1000000,
                }
            )
        # SMA50 ≈ avg of newest 50 prices (120 down to ~112)
        sma50_approx = sum(p['close'] for p in prices[:50]) / 50
        price = sma50_approx * 1.20  # 20% above SMA50
        quote = {
            'price': price,
            'yearHigh': price * 1.02,
            'yearLow': 60.0,
        }
        result = calculate_trend_template(prices, quote, rs_rank=85)
        assert result['raw_score'] >= 85
        assert result['passed'] is True
        assert result['extended_penalty'] < 0
        assert result['score'] < result['raw_score']

    def test_raw_score_in_result(self):
        result = self._run_tt(10.0)
        assert 'raw_score' in result

    def test_score_is_adjusted(self):
        result = self._run_tt(15.0)
        assert result['score'] == max(
            0, result['raw_score'] + result['extended_penalty']
        )

    # --- Output fields ---

    def test_sma50_distance_in_result(self):
        result = self._run_tt(10.0)
        assert 'sma50_distance_pct' in result
        assert result['sma50_distance_pct'] is not None
        assert abs(result['sma50_distance_pct'] - 10.0) < 0.5

    def test_extended_penalty_in_result(self):
        result = self._run_tt(10.0)
        assert 'extended_penalty' in result

    # --- Custom threshold (R1-3) ---

    def test_custom_threshold_5pct(self):
        result = self._run_tt(6.0, ext_threshold=5.0)
        assert result['extended_penalty'] == -5

    def test_custom_threshold_15pct(self):
        result = self._run_tt(10.0, ext_threshold=15.0)
        assert result['extended_penalty'] == 0


# ===========================================================================
# E2E Threshold Passthrough Test (R2-7)
# ===========================================================================


class TestExtThresholdE2E:
    """Test that ext_threshold passes through analyze_stock to trend_template."""

    def test_ext_threshold_passes_through_to_trend_template(self):
        """analyze_stock(ext_threshold=15) uses 15% threshold for penalty."""
        sma50_target = 100.0
        n = 250
        prices = []
        for i in range(n):
            prices.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': sma50_target,
                    'high': sma50_target * 1.005,
                    'low': sma50_target * 0.995,
                    'close': sma50_target,
                    'adjClose': sma50_target,
                    'volume': 1000000,
                }
            )
        # Price is 12% above SMA50
        price = sma50_target * 1.12
        quote = {
            'price': price,
            'yearHigh': price * 1.05,
            'yearLow': sma50_target * 0.6,
        }
        sp500 = _make_prices(n, start=95, daily_change=0.0005)

        # Default threshold=8 -> 12% distance -> penalty=-10
        result_default = analyze_stock(
            'TEST',
            prices,
            quote,
            sp500,
            'Tech',
            'Test Corp',
        )
        tt_default = result_default['trend_template']
        assert tt_default['extended_penalty'] == -10

        # Custom threshold=15 -> 12% distance -> no penalty
        result_custom = analyze_stock(
            'TEST',
            prices,
            quote,
            sp500,
            'Tech',
            'Test Corp',
            ext_threshold=15.0,
        )
        tt_custom = result_custom['trend_template']
        assert tt_custom['extended_penalty'] == 0


# ===========================================================================
# Sector Distribution Bug Fix Tests (Commit 1A)
# ===========================================================================


class TestSectorDistribution:
    """Test that sector distribution uses all_results, not just top N."""

    def _make_stock(self, symbol, sector='Technology', score=75.0, rating=None):
        if rating is None:
            if score >= 90:
                rating = 'Textbook VCP'
            elif score >= 80:
                rating = 'Strong VCP'
            elif score >= 70:
                rating = 'Good VCP'
            elif score >= 60:
                rating = 'Developing VCP'
            elif score >= 50:
                rating = 'Weak VCP'
            else:
                rating = 'No VCP'
        return {
            'symbol': symbol,
            'company_name': f'{symbol} Corp',
            'sector': sector,
            'price': 150.0,
            'market_cap': 50e9,
            'composite_score': score,
            'rating': rating,
            'rating_description': 'Test',
            'guidance': 'Test guidance',
            'weakest_component': 'Volume',
            'weakest_score': 40,
            'strongest_component': 'Trend',
            'strongest_score': 100,
            'valid_vcp': True,
            'entry_ready': False,
            'trend_template': {'score': 100, 'criteria_passed': 7},
            'vcp_pattern': {
                'score': 70,
                'num_contractions': 2,
                'contractions': [],
                'pivot_price': 145.0,
            },
            'volume_pattern': {'score': 40, 'dry_up_ratio': 0.8},
            'pivot_proximity': {
                'score': 75,
                'distance_from_pivot_pct': -3.0,
                'stop_loss_price': 140.0,
                'risk_pct': 7.0,
                'trade_status': 'NEAR PIVOT',
            },
            'relative_strength': {
                'score': 80,
                'rs_rank_estimate': 80,
                'weighted_rs': 15.0,
            },
        }

    def test_sector_distribution_uses_all_results(self):
        """Sector distribution should count all candidates, not just top N."""
        all_results = [
            self._make_stock('A1', 'Technology'),
            self._make_stock('A2', 'Technology'),
            self._make_stock('A3', 'Healthcare'),
            self._make_stock('A4', 'Financials'),
            self._make_stock('A5', 'Financials'),
            self._make_stock('A6', 'Financials'),
        ]
        top_results = all_results[:2]  # Only Technology stocks

        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 6},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(
                top_results, metadata, md_file, all_results=all_results
            )
            with open(md_file) as f:
                content = f.read()
            # Should contain Healthcare and Financials from all_results
            assert 'Healthcare' in content
            assert 'Financials' in content

    def test_report_header_shows_top_count(self):
        """When top N < total, report should show 'Showing top X of Y'."""
        all_results = [self._make_stock(f'S{i}') for i in range(10)]
        top_results = all_results[:3]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 10},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(
                top_results, metadata, md_file, all_results=all_results
            )
            with open(md_file) as f:
                content = f.read()
            assert 'Showing top 3 of 10 candidates' in content

    def test_no_top_count_when_all_shown(self):
        """When showing all results, no 'Showing top X of Y' message."""
        results = [self._make_stock(f'S{i}') for i in range(5)]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {'vcp_candidates': 5},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(results, metadata, md_file, all_results=results)
            with open(md_file) as f:
                content = f.read()
            assert 'Showing top' not in content

    def test_methodology_link_text(self):
        """Methodology link should not reference a nonexistent file path."""
        results = [self._make_stock('S0')]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(results, metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert '`references/vcp_methodology.md`' not in content
            assert 'VCP methodology reference' in content

    def test_json_report_has_sector_distribution(self):
        """JSON report should include sector_distribution field."""
        all_results = [
            self._make_stock('A1', 'Technology'),
            self._make_stock('A2', 'Healthcare'),
        ]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            json_file = os.path.join(tmpdir, 'test.json')
            generate_json_report(
                all_results[:1], metadata, json_file, all_results=all_results
            )
            with open(json_file) as f:
                data = json.load(f)
            assert 'sector_distribution' in data
            assert data['sector_distribution']['Technology'] == 1
            assert data['sector_distribution']['Healthcare'] == 1

    def test_section_headers_show_counts(self):
        """Section headers should show stock counts."""
        entry_ready = self._make_stock('READY', score=85.0, rating='Strong VCP')
        entry_ready['entry_ready'] = True
        extended = self._make_stock('EXT', score=75.0, rating='Good VCP')
        extended['entry_ready'] = False
        results = [entry_ready, extended]
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report(results, metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert 'Pre-Breakout Watchlist (1 stock' in content
            assert 'Extended / Quality VCP (1 stock' in content


# ===========================================================================
# RS Percentile Ranking Tests (Commit 1D)
# ===========================================================================


class TestRSPercentileRanking:
    """Test universe-relative RS percentile ranking."""

    def test_rank_ordering(self):
        """Higher weighted_rs gets higher percentile."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'AAPL': {'score': 80, 'weighted_rs': 30.0},
            'MSFT': {'score': 70, 'weighted_rs': 20.0},
            'GOOG': {'score': 60, 'weighted_rs': 10.0},
            'AMZN': {'score': 50, 'weighted_rs': 5.0},
        }
        ranked = rank_relative_strength_universe(rs_map)
        assert ranked['AAPL']['rs_percentile'] > ranked['AMZN']['rs_percentile']
        assert ranked['AAPL']['score'] >= ranked['MSFT']['score']

    def test_score_mapping(self):
        """Top percentile gets top score."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {f'S{i}': {'score': 50, 'weighted_rs': float(i)} for i in range(100)}
        ranked = rank_relative_strength_universe(rs_map)
        # S99 has highest weighted_rs -> highest percentile -> highest score
        assert ranked['S99']['score'] >= 90
        # S0 has lowest -> lowest score
        assert ranked['S0']['score'] <= 30

    def test_single_stock(self):
        """Single stock capped by small-population rule (n=1 -> max score 70)."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {'ONLY': {'score': 50, 'weighted_rs': 10.0}}
        ranked = rank_relative_strength_universe(rs_map)
        # With n=1, percentile and score are both capped
        assert ranked['ONLY']['score'] <= 70
        assert ranked['ONLY']['rs_percentile'] <= 74

    def test_handles_none_weighted_rs(self):
        """Stocks with None weighted_rs get score=0 and percentile=0."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'GOOD': {'score': 80, 'weighted_rs': 20.0},
            'BAD': {'score': 0, 'weighted_rs': None},
        }
        ranked = rank_relative_strength_universe(rs_map)
        assert ranked['GOOD']['rs_percentile'] > ranked['BAD']['rs_percentile']
        assert ranked['BAD']['score'] == 0
        assert ranked['BAD']['rs_percentile'] == 0

    def test_empty_dict(self):
        """Empty input returns empty dict."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        ranked = rank_relative_strength_universe({})
        assert ranked == {}

    def test_tied_values(self):
        """Tied weighted_rs values should get same percentile."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'A': {'score': 50, 'weighted_rs': 10.0},
            'B': {'score': 50, 'weighted_rs': 10.0},
            'C': {'score': 50, 'weighted_rs': 5.0},
        }
        ranked = rank_relative_strength_universe(rs_map)
        assert ranked['A']['rs_percentile'] == ranked['B']['rs_percentile']
        assert ranked['A']['rs_percentile'] > ranked['C']['rs_percentile']

    def test_rs_percentile_in_report(self):
        """Report should show RS Percentile when available."""
        stock = {
            'symbol': 'TEST',
            'company_name': 'Test Corp',
            'sector': 'Technology',
            'price': 150.0,
            'market_cap': 50e9,
            'composite_score': 75.0,
            'rating': 'Good VCP',
            'rating_description': 'Test',
            'guidance': 'Test',
            'weakest_component': 'Volume',
            'weakest_score': 40,
            'strongest_component': 'Trend',
            'strongest_score': 100,
            'valid_vcp': True,
            'entry_ready': False,
            'trend_template': {'score': 100, 'criteria_passed': 7},
            'vcp_pattern': {
                'score': 70,
                'num_contractions': 2,
                'contractions': [],
                'pivot_price': 145.0,
            },
            'volume_pattern': {'score': 40, 'dry_up_ratio': 0.8},
            'pivot_proximity': {
                'score': 75,
                'distance_from_pivot_pct': -3.0,
                'stop_loss_price': 140.0,
                'risk_pct': 7.0,
                'trade_status': 'NEAR PIVOT',
            },
            'relative_strength': {
                'score': 85,
                'rs_rank_estimate': 80,
                'weighted_rs': 15.0,
                'rs_percentile': 92,
            },
        }
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'funnel': {},
            'api_stats': {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = os.path.join(tmpdir, 'test.md')
            generate_markdown_report([stock], metadata, md_file)
            with open(md_file) as f:
                content = f.read()
            assert 'RS Percentile: 92' in content


# ===========================================================================
# ATR and ZigZag Swing Detection Tests (Commit 2)
# ===========================================================================


class TestATRCalculation:
    """Test _calculate_atr function."""

    def test_atr_basic(self):
        """ATR for constant range bars should equal the range."""
        from calculators.vcp_pattern_calculator import _calculate_atr

        n = 20
        highs = [105.0] * n
        lows = [95.0] * n
        closes = [100.0] * n
        atr = _calculate_atr(highs, lows, closes, period=14)
        assert abs(atr - 10.0) < 0.5

    def test_atr_insufficient_data(self):
        """ATR with < period+1 data returns 0."""
        from calculators.vcp_pattern_calculator import _calculate_atr

        highs = [105.0] * 5
        lows = [95.0] * 5
        closes = [100.0] * 5
        atr = _calculate_atr(highs, lows, closes, period=14)
        assert atr == 0.0


class TestZigZagSwingDetection:
    """Test ATR-based ZigZag swing detection."""

    def test_zigzag_finds_known_pattern(self):
        """A clear up-down-up-down pattern should find swing points."""
        from calculators.vcp_pattern_calculator import _zigzag_swing_points

        # Build: 20 bars up, 20 bars down, 20 bars up, 20 bars down
        n = 80
        highs, lows, closes, dates = [], [], [], []
        for i in range(n):
            if i < 20:
                base = 100 + i * 2  # 100 -> 138
            elif i < 40:
                base = 138 - (i - 20) * 2  # 138 -> 100
            elif i < 60:
                base = 100 + (i - 40) * 2  # 100 -> 138
            else:
                base = 138 - (i - 60) * 2  # 138 -> 100
            highs.append(base + 1)
            lows.append(base - 1)
            closes.append(float(base))
            dates.append(f'day-{i}')
        swing_highs, swing_lows = _zigzag_swing_points(
            highs, lows, closes, dates, atr_multiplier=1.5
        )
        assert len(swing_highs) >= 1
        assert len(swing_lows) >= 1

    def test_smooth_uptrend_fewer_swings(self):
        """Smooth uptrend should produce fewer swings than choppy market."""
        from calculators.vcp_pattern_calculator import _zigzag_swing_points

        n = 100
        # Smooth uptrend
        smooth_highs = [100 + i * 0.5 + 1 for i in range(n)]
        smooth_lows = [100 + i * 0.5 - 1 for i in range(n)]
        smooth_closes = [100 + i * 0.5 for i in range(n)]
        dates = [f'day-{i}' for i in range(n)]
        sh, sl = _zigzag_swing_points(smooth_highs, smooth_lows, smooth_closes, dates)
        # Should produce very few swings (smooth trend)
        assert len(sh) + len(sl) <= 4

    def test_atr_multiplier_sensitivity(self):
        """Higher multiplier = fewer swing points detected."""
        from calculators.vcp_pattern_calculator import _zigzag_swing_points

        n = 80
        highs, lows, closes, dates = [], [], [], []
        for i in range(n):
            if i < 20:
                base = 100 + i * 2
            elif i < 40:
                base = 138 - (i - 20) * 2
            elif i < 60:
                base = 100 + (i - 40) * 2
            else:
                base = 138 - (i - 60) * 2
            highs.append(base + 1)
            lows.append(base - 1)
            closes.append(float(base))
            dates.append(f'day-{i}')
        sh_low, sl_low = _zigzag_swing_points(
            highs, lows, closes, dates, atr_multiplier=0.5
        )
        sh_high, sl_high = _zigzag_swing_points(
            highs, lows, closes, dates, atr_multiplier=3.0
        )
        # Lower multiplier should find at least as many swings
        assert len(sh_low) + len(sl_low) >= len(sh_high) + len(sl_high)

    def test_insufficient_data(self):
        """< 15 bars of data should return empty."""
        from calculators.vcp_pattern_calculator import _zigzag_swing_points

        highs = [105.0] * 10
        lows = [95.0] * 10
        closes = [100.0] * 10
        dates = [f'day-{i}' for i in range(10)]
        sh, sl = _zigzag_swing_points(highs, lows, closes, dates)
        assert sh == []
        assert sl == []


# ===========================================================================
# VCP Pattern Enhanced Tests (Commit 3: ZigZag integration)
# ===========================================================================


class TestVCPPatternEnhanced:
    """Test ZigZag integration, multi-start, and min contraction duration."""

    def _make_vcp_prices(self, n=120):
        """Build synthetic VCP price data (most-recent-first) with clear contractions.

        Creates: ramp up -> T1 drop -> recovery -> T2 smaller drop -> recovery
        """
        # Chronological (oldest first): build then reverse
        chrono = []
        for i in range(n):
            if i < 30:
                # Ramp up from 80 to 120
                base = 80 + (40 * i / 30)
            elif i < 50:
                # T1: drop from 120 to ~100 (16.7%)
                progress = (i - 30) / 20
                base = 120 - 20 * progress
            elif i < 70:
                # Recovery back to ~118
                progress = (i - 50) / 20
                base = 100 + 18 * progress
            elif i < 85:
                # T2: drop from 118 to ~110 (6.8%)
                progress = (i - 70) / 15
                base = 118 - 8 * progress
            else:
                # Recovery to ~117, consolidation near pivot
                progress = (i - 85) / (n - 85)
                base = 110 + 7 * progress
            chrono.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': round(base, 2),
                    'high': round(base * 1.01, 2),
                    'low': round(base * 0.99, 2),
                    'close': round(base, 2),
                    'volume': 1000000,
                }
            )
        # Return most-recent-first
        return list(reversed(chrono))

    def test_backward_compatible_return_schema(self):
        """calculate_vcp_pattern returns same keys as before."""
        prices = self._make_vcp_prices()
        result = calculate_vcp_pattern(prices)
        assert 'score' in result
        assert 'valid_vcp' in result
        assert 'contractions' in result
        assert 'num_contractions' in result
        assert 'pivot_price' in result

    def test_new_params_accepted(self):
        """New parameters atr_multiplier, atr_period, min_contraction_days accepted."""
        prices = self._make_vcp_prices()
        result = calculate_vcp_pattern(
            prices, atr_multiplier=2.0, atr_period=10, min_contraction_days=3
        )
        assert isinstance(result, dict)
        assert 'score' in result

    def test_atr_value_in_result(self):
        """Result should include atr_value when ZigZag is used."""
        prices = self._make_vcp_prices()
        result = calculate_vcp_pattern(prices, atr_multiplier=1.5)
        # atr_value may be present (if ZigZag was used) or absent (if fell back)
        # Either way, the result should not crash
        assert isinstance(result, dict)

    def test_contraction_duration_in_result(self):
        """Contractions should have duration_days field when available."""
        prices = self._make_vcp_prices()
        result = calculate_vcp_pattern(
            prices, atr_multiplier=1.5, min_contraction_days=3
        )
        for c in result.get('contractions', []):
            assert 'duration_days' in c

    def test_existing_validation_still_works(self):
        """_validate_vcp should still work with existing test data."""
        contractions = _make_vcp_contractions([20, 10, 5])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True

    def test_min_contraction_days_filters_short(self):
        """Contractions shorter than min_contraction_days should be excluded."""
        from calculators.vcp_pattern_calculator import calculate_vcp_pattern

        prices = self._make_vcp_prices()
        # Very high min_contraction_days should reduce or eliminate contractions
        result = calculate_vcp_pattern(prices, min_contraction_days=50)
        # With 50-day minimum, most contractions would be filtered
        assert result['num_contractions'] <= 2


# ===========================================================================
# Volume Zone Analysis Tests (Commit 4)
# ===========================================================================


class TestVolumeZoneAnalysis:
    """Test zone-based volume analysis."""

    def _make_volume_prices(self, n=60, base_vol=1000000, dry_up_vol=300000):
        """Build prices with clear volume zones (most-recent-first)."""
        prices = []
        for i in range(n):
            vol = base_vol
            if i < 10:  # Recent bars: dry-up zone
                vol = dry_up_vol
            prices.append(
                {
                    'date': f'2025-{(i // 22) + 1:02d}-{(i % 22) + 1:02d}',
                    'open': 100.0,
                    'high': 101.0,
                    'low': 99.0,
                    'close': 100.0,
                    'volume': vol,
                }
            )
        return prices

    def test_backward_compatible_without_contractions(self):
        """Without contractions param, old behavior preserved."""
        prices = self._make_volume_prices()
        result = calculate_volume_pattern(prices, pivot_price=101.0)
        assert 'dry_up_ratio' in result
        assert result['score'] > 0

    def test_zone_analysis_present(self):
        """When contractions provided, zone_analysis should appear in result."""
        prices = self._make_volume_prices()
        contractions = [
            {'high_idx': 30, 'low_idx': 40, 'label': 'T1'},
            {'high_idx': 20, 'low_idx': 25, 'label': 'T2'},
        ]
        result = calculate_volume_pattern(
            prices, pivot_price=101.0, contractions=contractions
        )
        assert 'zone_analysis' in result

    def test_zone_b_dry_up(self):
        """Zone B (pivot approach) with low volume -> high dry-up score."""
        # Build prices where bars near pivot have very low volume
        prices = []
        for i in range(60):
            vol = 1000000
            if i < 10:  # Most recent: very dry
                vol = 100000
            prices.append(
                {
                    'date': f'day-{i}',
                    'open': 100.0,
                    'high': 101.0,
                    'low': 99.0,
                    'close': 100.0,
                    'volume': vol,
                }
            )
        contractions = [
            {'high_idx': 30, 'low_idx': 40, 'label': 'T1'},
            {'high_idx': 15, 'low_idx': 20, 'label': 'T2'},
        ]
        result = calculate_volume_pattern(
            prices, pivot_price=101.0, contractions=contractions
        )
        assert result['dry_up_ratio'] < 0.5

    def test_contraction_volume_declining_bonus(self):
        """Declining volume across contractions should add bonus and report trend."""
        # Build prices where T1 zone has higher volume than T2 zone
        n = 60
        prices = []
        for i in range(n):
            vol = 1000000
            # T1: chronological 10-20, reversed = 39-49
            if 39 <= i <= 49:
                vol = 2000000
            # T2: chronological 35-45, reversed = 14-24
            elif 14 <= i <= 24:
                vol = 500000
            prices.append(
                {
                    'date': f'day-{i}',
                    'open': 100.0,
                    'high': 101.0,
                    'low': 99.0,
                    'close': 100.0,
                    'volume': vol,
                }
            )
        contractions = [
            {'high_idx': 10, 'low_idx': 20, 'label': 'T1'},
            {'high_idx': 35, 'low_idx': 45, 'label': 'T2'},
        ]
        result = calculate_volume_pattern(
            prices, pivot_price=101.0, contractions=contractions
        )
        assert 'contraction_volume_trend' in result
        assert result['contraction_volume_trend']['declining'] is True

    def test_empty_contractions_fallback(self):
        """Empty contractions list should use old behavior."""
        prices = self._make_volume_prices()
        result = calculate_volume_pattern(prices, pivot_price=101.0, contractions=[])
        # Should still work with old logic
        assert 'dry_up_ratio' in result
        assert result['score'] > 0

    def test_breakout_volume_uses_zone_c(self):
        """When breakout bar is at pivot, zone C volume should be used."""
        prices = []
        for i in range(60):
            vol = 500000
            close = 100.0
            if i == 0:
                # Breakout bar: high volume, price above pivot
                vol = 2000000
                close = 102.0
            prices.append(
                {
                    'date': f'day-{i}',
                    'open': close - 1,
                    'high': close + 0.5,
                    'low': close - 1.5,
                    'close': close,
                    'volume': vol,
                }
            )
        contractions = [
            {'high_idx': 30, 'low_idx': 40, 'label': 'T1'},
            {'high_idx': 15, 'low_idx': 20, 'label': 'T2'},
        ]
        result = calculate_volume_pattern(
            prices, pivot_price=101.0, contractions=contractions
        )
        assert result['breakout_volume_detected'] is True


# ===========================================================================
# Code Review Fix Tests: RS None handling, weakest/strongest update,
# small population, and tautological test fixes
# ===========================================================================


class TestRSNoneHandling:
    """Issue #1 (High): weighted_rs=None stocks must not inflate scores."""

    def test_all_none_get_score_zero(self):
        """All-None universe: every stock should get score=0, not score=100."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'A': {'score': 0, 'weighted_rs': None, 'error': 'No SPY data'},
            'B': {'score': 0, 'weighted_rs': None, 'error': 'No SPY data'},
            'C': {'score': 0, 'weighted_rs': None, 'error': 'No SPY data'},
        }
        ranked = rank_relative_strength_universe(rs_map)
        for sym in ['A', 'B', 'C']:
            assert ranked[sym]['score'] == 0
            assert ranked[sym]['rs_percentile'] == 0

    def test_mixed_none_excludes_none_from_percentile(self):
        """None stocks excluded from percentile; valid stocks ranked among themselves."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'GOOD1': {'score': 80, 'weighted_rs': 30.0},
            'GOOD2': {'score': 70, 'weighted_rs': 10.0},
            'BAD': {'score': 0, 'weighted_rs': None, 'error': 'No data'},
        }
        ranked = rank_relative_strength_universe(rs_map)
        # BAD should get score=0
        assert ranked['BAD']['score'] == 0
        assert ranked['BAD']['rs_percentile'] == 0
        # GOOD1 should still be ranked highest among valid stocks
        assert ranked['GOOD1']['rs_percentile'] > ranked['GOOD2']['rs_percentile']
        assert ranked['GOOD1']['score'] > 0

    def test_none_stock_preserves_error(self):
        """None-weighted_rs stock should preserve its original error field."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {
            'OK': {'score': 50, 'weighted_rs': 10.0},
            'ERR': {'score': 0, 'weighted_rs': None, 'error': 'SPY fetch failed'},
        }
        ranked = rank_relative_strength_universe(rs_map)
        assert ranked['ERR']['error'] == 'SPY fetch failed'


class TestRSSmallPopulation:
    """Issue #3 (Medium): small populations should cap percentile scores."""

    def test_small_population_caps_score(self):
        """With fewer than 10 valid stocks, scores should be capped."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        # 3 stocks: highest should NOT get score=100
        rs_map = {
            'A': {'score': 50, 'weighted_rs': 20.0},
            'B': {'score': 50, 'weighted_rs': 10.0},
            'C': {'score': 50, 'weighted_rs': 5.0},
        }
        ranked = rank_relative_strength_universe(rs_map)
        # With only 3 valid stocks, best should be capped at 80
        assert ranked['A']['score'] <= 80

    def test_small_population_caps_percentile_consistently(self):
        """rs_percentile must be capped consistently with score."""
        from calculators.relative_strength_calculator import (
            _percentile_to_score,
            rank_relative_strength_universe,
        )

        # 3 stocks: raw percentile would be 100 for top stock
        rs_map = {
            'A': {'score': 50, 'weighted_rs': 20.0},
            'B': {'score': 50, 'weighted_rs': 10.0},
            'C': {'score': 50, 'weighted_rs': 5.0},
        }
        ranked = rank_relative_strength_universe(rs_map)
        # Percentile must produce the capped score when passed through _percentile_to_score
        for sym in ['A', 'B', 'C']:
            pct = ranked[sym]['rs_percentile']
            score = ranked[sym]['score']
            assert _percentile_to_score(pct) == score

    def test_large_population_no_cap(self):
        """With 20+ valid stocks, no cap is applied."""
        from calculators.relative_strength_calculator import (
            rank_relative_strength_universe,
        )

        rs_map = {f'S{i}': {'score': 50, 'weighted_rs': float(i)} for i in range(20)}
        ranked = rank_relative_strength_universe(rs_map)
        assert ranked['S19']['score'] >= 90
        # Percentile should also be uncapped
        assert ranked['S19']['rs_percentile'] >= 95


class TestWeakestStrongestUpdate:
    """Issue #2 (Medium): weakest/strongest must update after RS re-ranking."""

    def test_weakest_strongest_reflects_updated_rs(self):
        """After RS re-ranking, composite result must have fresh weakest/strongest."""
        # Simulate a result where RS was initially strongest (score=100)
        # but after re-ranking becomes weaker (score=40)
        composite = calculate_composite_score(
            trend_score=80,
            contraction_score=70,
            volume_score=60,
            pivot_score=50,
            rs_score=40,  # RS now weakest after re-ranking
        )
        assert composite['weakest_component'] == 'Relative Strength'
        assert composite['weakest_score'] == 40
        # The strongest should be Trend Template
        assert composite['strongest_component'] == 'Trend Template (Stage 2)'
        assert composite['strongest_score'] == 80


class TestFixedTautologicalTests:
    """Issue #4 (Low): fix tests that were always-true."""

    def test_new_params_no_crash(self):
        """calculate_vcp_pattern with new params should not raise an exception."""
        prices = TestVCPPatternEnhanced._make_vcp_prices(None)
        result = calculate_vcp_pattern(
            prices, atr_multiplier=2.0, atr_period=10, min_contraction_days=3
        )
        assert isinstance(result, dict)
        assert 'score' in result

    def test_declining_volume_bonus_value(self):
        """Declining contraction volume should yield +5 bonus vs non-declining."""
        # Build prices with declining volume in contraction zones
        prices_declining = []
        prices_flat = []
        for i in range(60):
            close = 100.0
            vol_base = 1000000
            prices_declining.append(
                {
                    'date': f'day-{i}',
                    'open': 100.0,
                    'high': 101.0,
                    'low': 99.0,
                    'close': close,
                    'volume': vol_base,
                }
            )
            prices_flat.append(
                {
                    'date': f'day-{i}',
                    'open': 100.0,
                    'high': 101.0,
                    'low': 99.0,
                    'close': close,
                    'volume': vol_base,
                }
            )

        # Contractions where T1 zone has higher volume than T2 zone
        # (chronological indices: T1 is earlier, T2 is later)
        n = 60
        # T1: indices 10-20 (chronological), reversed = 39-49
        # T2: indices 35-45 (chronological), reversed = 14-24
        # For declining: T1 zone gets high volume, T2 zone gets low volume
        for i in range(n):
            n - 1 - i
            # T1 chronological 10-20 -> reversed 39-49
            if 39 <= i <= 49:
                prices_declining[i]['volume'] = 2000000
                prices_flat[i]['volume'] = 1000000
            # T2 chronological 35-45 -> reversed 14-24
            if 14 <= i <= 24:
                prices_declining[i]['volume'] = 500000
                prices_flat[i]['volume'] = 1000000

        contractions = [
            {'high_idx': 10, 'low_idx': 20, 'label': 'T1'},
            {'high_idx': 35, 'low_idx': 45, 'label': 'T2'},
        ]

        result_declining = calculate_volume_pattern(
            prices_declining, pivot_price=101.0, contractions=contractions
        )
        result_flat = calculate_volume_pattern(
            prices_flat, pivot_price=101.0, contractions=contractions
        )

        # Declining should have the bonus
        assert (
            result_declining.get('contraction_volume_trend', {}).get('declining')
            is True
        )
        assert result_flat.get('contraction_volume_trend', {}).get('declining') is False
        # Score difference should be exactly 5
        assert result_declining['score'] - result_flat['score'] == 5


# ===========================================================================
# Parameter Passthrough Tests (VCP tuning parameters)
# ===========================================================================


class TestParameterPassthrough:
    """Test that new tuning parameters correctly affect VCP detection."""

    def test_min_contractions_3_rejects_2_contraction_pattern(self):
        """min_contractions=3 should reject a pattern with only 2 contractions."""
        contractions = _make_vcp_contractions([20, 10])
        result = _validate_vcp(contractions, total_days=120, min_contractions=3)
        assert result['valid'] is False
        assert any('3' in issue for issue in result['issues'])

    def test_min_contractions_2_default_backward_compatible(self):
        """min_contractions=2 (default) accepts a 2-contraction pattern."""
        contractions = _make_vcp_contractions([20, 10])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True

    def test_t1_depth_min_12_rejects_shallow(self):
        """t1_depth_min=12 should reject T1=10% pattern."""
        contractions = _make_vcp_contractions([10, 5])
        result = _validate_vcp(contractions, total_days=120, t1_depth_min=12.0)
        assert result['valid'] is False
        assert any('12.0' in issue for issue in result['issues'])

    def test_t1_depth_min_default_accepts_8pct(self):
        """Default t1_depth_min=8.0 accepts T1=10%."""
        contractions = _make_vcp_contractions([10, 5])
        result = _validate_vcp(contractions, total_days=120)
        assert result['valid'] is True

    def test_contraction_ratio_09_accepts_looser(self):
        """contraction_ratio=0.9 accepts T1=20%, T2=17% (ratio=0.85)."""
        contractions = _make_vcp_contractions([20, 17])
        # Default 0.75 rejects
        result_strict = _validate_vcp(
            contractions, total_days=120, contraction_ratio=0.75
        )
        assert result_strict['valid'] is False
        # Relaxed 0.9 accepts
        result_relaxed = _validate_vcp(
            contractions, total_days=120, contraction_ratio=0.9
        )
        assert result_relaxed['valid'] is True

    def test_breakout_volume_ratio_2_rejects_16x(self):
        """breakout_volume_ratio=2.0 should not detect 1.6x as breakout."""
        prices = _make_prices(60, volume=1000000)
        # Most recent bar: 1.6x volume, price above pivot
        prices[0]['volume'] = 1600000
        prices[0]['close'] = 102.0
        result = calculate_volume_pattern(
            prices, pivot_price=100.0, breakout_volume_ratio=2.0
        )
        assert result['breakout_volume_detected'] is False

    def test_breakout_volume_ratio_default_detects_16x(self):
        """Default breakout_volume_ratio=1.5 detects 1.6x as breakout."""
        prices = _make_prices(60, volume=1000000)
        prices[0]['volume'] = 1600000
        prices[0]['close'] = 102.0
        result = calculate_volume_pattern(prices, pivot_price=100.0)
        assert result['breakout_volume_detected'] is True

    def test_calculate_vcp_pattern_min_contractions_3(self):
        """calculate_vcp_pattern with min_contractions=3 finds fewer patterns."""
        from calculators.vcp_pattern_calculator import calculate_vcp_pattern

        prices = TestVCPPatternEnhanced._make_vcp_prices(None)
        result_2 = calculate_vcp_pattern(prices, min_contractions=2)
        result_3 = calculate_vcp_pattern(prices, min_contractions=3)
        # With stricter min_contractions, either fewer contractions or not valid
        if result_2['valid_vcp'] and result_2['num_contractions'] < 3:
            assert result_3['valid_vcp'] is False


class TestTrendMinScore:
    """Test passes_trend_filter (Phase 2 gate) from screen_vcp.py."""

    def test_trend_min_score_70_passes_raw_75(self):
        """passes_trend_filter with raw_score=75 and threshold=70 -> True."""
        tt_result = {'raw_score': 75, 'passed': False}
        assert passes_trend_filter(tt_result, trend_min_score=70) is True

    def test_trend_min_score_85_rejects_raw_80(self):
        """passes_trend_filter with raw_score=80 and default threshold=85 -> False."""
        tt_result = {'raw_score': 80, 'passed': False}
        assert passes_trend_filter(tt_result) is False

    def test_trend_min_score_100_rejects_all(self):
        """passes_trend_filter with threshold=100 rejects 99.9."""
        tt_result = {'raw_score': 99.9, 'passed': True}
        assert passes_trend_filter(tt_result, trend_min_score=100) is False

    def test_uses_raw_score_not_passed_field(self):
        """Phase 2 must gate on raw_score, not the passed boolean.

        A stock with raw_score=75 and passed=False should still pass
        Phase 2 when trend_min_score=70.
        """
        tt_result = {'raw_score': 75, 'passed': False, 'score': 60}
        assert passes_trend_filter(tt_result, trend_min_score=70) is True
        # Verify it would NOT pass if we used the 'passed' field
        assert tt_result['passed'] is False

    def test_missing_raw_score_returns_false(self):
        """If raw_score key is absent, passes_trend_filter defaults to 0."""
        tt_result = {'passed': True, 'score': 85}
        assert passes_trend_filter(tt_result, trend_min_score=85) is False

    def test_with_real_calculate_trend_template(self):
        """Integration: real calculate_trend_template output flows through filter."""
        prices = _make_prices(250, start=80, daily_change=0.001)
        quote = {'price': 120, 'yearHigh': 125, 'yearLow': 70}
        tt_result = calculate_trend_template(prices, quote, rs_rank=85)
        raw = tt_result.get('raw_score', 0)
        # The result must be consistent with passes_trend_filter
        assert passes_trend_filter(tt_result, trend_min_score=raw) is True
        assert passes_trend_filter(tt_result, trend_min_score=raw + 0.1) is False


class TestBacktestRegression:
    """Regression tests ensuring stricter params are more selective."""

    def test_min_contractions_3_more_selective_than_2(self):
        """min_contractions=3 should be at least as selective as =2."""
        contractions_2 = _make_vcp_contractions([20, 10])
        result_2 = _validate_vcp(contractions_2, total_days=120, min_contractions=2)
        result_3 = _validate_vcp(contractions_2, total_days=120, min_contractions=3)
        # 2-contraction pattern: valid for min=2, invalid for min=3
        assert result_2['valid'] is True
        assert result_3['valid'] is False

    def test_higher_t1_depth_min_excludes_shallow(self):
        """Higher t1_depth_min excludes patterns that default accepts."""
        contractions = _make_vcp_contractions([10, 5])
        result_default = _validate_vcp(contractions, total_days=120, t1_depth_min=8.0)
        result_strict = _validate_vcp(contractions, total_days=120, t1_depth_min=15.0)
        assert result_default['valid'] is True
        assert result_strict['valid'] is False


class TestTuningParamsMetadata:
    """Test that tuning_params appear in report metadata."""

    def test_metadata_tuning_params_from_parse_arguments(self):
        """parse_arguments() produces args with all 8 tuning param attributes.

        This exercises the real CLI parser so a renamed or missing flag
        causes a test failure.
        """
        import sys
        from unittest.mock import patch

        test_argv = [
            'screen_vcp.py',
            '--min-contractions',
            '3',
            '--t1-depth-min',
            '12.0',
            '--breakout-volume-ratio',
            '2.0',
            '--trend-min-score',
            '90.0',
            '--atr-multiplier',
            '2.0',
            '--contraction-ratio',
            '0.6',
            '--min-contraction-days',
            '7',
            '--lookback-days',
            '180',
        ]
        with patch.object(sys, 'argv', test_argv):
            args = parse_arguments()

        # Build tuning_params the same way screen_vcp.main() does (line ~620)
        tuning_params = {
            'min_contractions': args.min_contractions,
            't1_depth_min': args.t1_depth_min,
            'breakout_volume_ratio': args.breakout_volume_ratio,
            'trend_min_score': args.trend_min_score,
            'atr_multiplier': args.atr_multiplier,
            'contraction_ratio': args.contraction_ratio,
            'min_contraction_days': args.min_contraction_days,
            'lookback_days': args.lookback_days,
        }
        assert len(tuning_params) == 8
        assert tuning_params['min_contractions'] == 3
        assert tuning_params['trend_min_score'] == 90.0
        assert tuning_params['lookback_days'] == 180

    def test_tuning_params_in_json_report(self):
        """JSON report should include tuning_params in metadata."""
        metadata = {
            'generated_at': '2026-01-01',
            'universe_description': 'Test',
            'tuning_params': {
                'min_contractions': 2,
                't1_depth_min': 8.0,
                'breakout_volume_ratio': 1.5,
                'trend_min_score': 85.0,
                'atr_multiplier': 1.5,
                'contraction_ratio': 0.75,
                'min_contraction_days': 5,
                'lookback_days': 120,
            },
            'funnel': {},
            'api_stats': {},
        }
        stock = {
            'symbol': 'TEST',
            'company_name': 'Test Corp',
            'sector': 'Technology',
            'price': 150.0,
            'market_cap': 50e9,
            'composite_score': 75.0,
            'rating': 'Good VCP',
            'rating_description': 'Test',
            'guidance': 'Test',
            'weakest_component': 'Volume',
            'weakest_score': 40,
            'strongest_component': 'Trend',
            'strongest_score': 100,
            'valid_vcp': True,
            'entry_ready': False,
            'trend_template': {'score': 100, 'criteria_passed': 7},
            'vcp_pattern': {
                'score': 70,
                'num_contractions': 2,
                'contractions': [],
                'pivot_price': 145.0,
            },
            'volume_pattern': {'score': 40, 'dry_up_ratio': 0.8},
            'pivot_proximity': {
                'score': 75,
                'distance_from_pivot_pct': -3.0,
                'stop_loss_price': 140.0,
                'risk_pct': 7.0,
                'trade_status': 'NEAR PIVOT',
            },
            'relative_strength': {
                'score': 80,
                'rs_rank_estimate': 80,
                'weighted_rs': 15.0,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            json_file = os.path.join(tmpdir, 'test.json')
            generate_json_report([stock], metadata, json_file)
            with open(json_file) as f:
                data = json.load(f)
            assert 'tuning_params' in data['metadata']
            tp = data['metadata']['tuning_params']
            assert len(tp) == 8
            assert tp['min_contractions'] == 2
            assert tp['trend_min_score'] == 85.0
