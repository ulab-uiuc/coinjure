"""Quantitative validation tools for market relations.

Provides statistical tests to confirm that a discovered relation
(e.g., from LLM discovery) produces a tradable, mean-reverting spread.

Requires the ``quant`` extra: ``pip install coinjure[quant]``
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

from coinjure.market.relations import ValidationResult

logger = logging.getLogger(__name__)


def _require_statsmodels():
    try:
        import statsmodels  # noqa: F401

        return True
    except ImportError:
        raise ImportError(
            'statsmodels is required for quantitative validation. '
            'Install with: pip install coinjure[quant]'
        )


def _require_scipy():
    try:
        import scipy  # noqa: F401

        return True
    except ImportError:
        raise ImportError(
            'scipy is required for quantitative validation. '
            'Install with: pip install coinjure[quant]'
        )


# ── Hedge Ratio ──────────────────────────────────────────────────────────


def estimate_hedge_ratio(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
) -> float:
    """Estimate the hedge ratio (beta) via OLS: prices_a = alpha + beta * prices_b.

    Returns beta. For a simple spread, beta ≈ 1.0 for same-event pairs.
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    y = np.array(prices_a, dtype=float)
    x = add_constant(np.array(prices_b, dtype=float))
    result = OLS(y, x).fit()
    return float(result.params[1])


def rolling_hedge_ratio(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    window: int = 30,
) -> list[float]:
    """Rolling OLS hedge ratio over a sliding window.

    Returns a list of hedge ratios (one per observation, NaN for warmup).
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)
    n = len(a)
    ratios = [float('nan')] * n

    for i in range(window, n):
        y = a[i - window : i]
        x = add_constant(b[i - window : i])
        try:
            result = OLS(y, x).fit()
            ratios[i] = float(result.params[1])
        except Exception:
            ratios[i] = float('nan')
    return ratios


# ── Spread Construction ──────────────────────────────────────────────────


def compute_spread(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    hedge_ratio: float = 1.0,
) -> list[float]:
    """Compute the spread: s_t = prices_a_t - hedge_ratio * prices_b_t."""
    return [a - hedge_ratio * b for a, b in zip(prices_a, prices_b)]


# ── Stationarity Tests ───────────────────────────────────────────────────


def adf_test(
    series: Sequence[float],
    significance: float = 0.05,
) -> tuple[float, float, bool]:
    """Augmented Dickey-Fuller test for stationarity.

    Returns (test_statistic, p_value, is_stationary).
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.tsa.stattools import adfuller

    arr = np.array(series, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 20:
        logger.warning('ADF test: insufficient data points (%d < 20)', len(arr))
        return 0.0, 1.0, False

    result = adfuller(arr, autolag='AIC')
    stat, pvalue = float(result[0]), float(result[1])
    return stat, pvalue, pvalue < significance


def kpss_test(
    series: Sequence[float],
    significance: float = 0.05,
) -> tuple[float, float, bool]:
    """KPSS test for stationarity (null = stationary).

    Returns (test_statistic, p_value, is_stationary).
    Note: KPSS null is stationarity, so is_stationary = (pvalue >= significance).
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.tsa.stattools import kpss as kpss_test_fn

    arr = np.array(series, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 20:
        return 0.0, 1.0, False

    stat, pvalue, _lags, _crit = kpss_test_fn(arr, regression='c', nlags='auto')
    return float(stat), float(pvalue), float(pvalue) >= significance


# ── Cointegration ────────────────────────────────────────────────────────


def engle_granger_test(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    significance: float = 0.05,
) -> tuple[float, float, bool]:
    """Engle-Granger two-step cointegration test.

    Step 1: OLS regression prices_a ~ prices_b
    Step 2: ADF test on the residuals

    Returns (test_statistic, p_value, is_cointegrated).
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.tsa.stattools import coint

    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)

    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if len(a) < 30:
        logger.warning('Cointegration test: insufficient data (%d < 30)', len(a))
        return 0.0, 1.0, False

    stat, pvalue, _crit = coint(a, b)
    return float(stat), float(pvalue), float(pvalue) < significance


# ── Half-Life (Ornstein-Uhlenbeck) ───────────────────────────────────────


def estimate_half_life(spread: Sequence[float]) -> float:
    """Estimate the half-life of mean reversion via OU process.

    Fits: delta_s_t = phi * (s_{t-1} - mean) + epsilon
    Half-life = -ln(2) / ln(1 + phi)

    Returns half-life in bars (NaN if not mean-reverting).
    """
    _require_statsmodels()
    import numpy as np
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    s = np.array(spread, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) < 10:
        return float('nan')

    delta = np.diff(s)
    lagged = s[:-1]
    x = add_constant(lagged)
    result = OLS(delta, x).fit()
    phi = float(result.params[1])

    if phi >= 0 or phi <= -1:
        return float('nan')  # not mean-reverting or unstable

    half_life = -math.log(2) / math.log(1 + phi)
    return half_life


# ── Correlation ──────────────────────────────────────────────────────────


def pearson_correlation(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
) -> float:
    """Compute Pearson correlation between two price series."""
    _require_scipy()
    import numpy as np
    from scipy.stats import pearsonr

    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if len(a) < 5:
        return 0.0
    corr, _ = pearsonr(a, b)
    return float(corr)


# ── Lead-Lag Detection ──────────────────────────────────────────────────


def detect_lead_lag(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    max_lag: int = 10,
) -> tuple[int, float]:
    """Detect lead-lag via cross-correlation on returns.

    Computes corr(returns_a[t], returns_b[t+k]) for k in [-max_lag, max_lag].
    Positive result means A leads B by that many steps.

    Returns (optimal_lag, correlation_at_optimal_lag).
    """
    import numpy as np

    a = np.array(prices_a, dtype=float)
    b = np.array(prices_b, dtype=float)
    n = min(len(a), len(b))
    if n < max_lag + 5:
        return 0, 0.0

    a, b = a[:n], b[:n]
    ra = np.diff(a)
    rb = np.diff(b)

    best_lag = 0
    best_corr = 0.0

    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x = ra[:len(ra) - lag] if lag > 0 else ra
            y = rb[lag:] if lag > 0 else rb
        else:
            x = ra[-lag:]
            y = rb[:len(rb) + lag]

        overlap = min(len(x), len(y))
        if overlap < 5:
            continue
        x, y = x[:overlap], y[:overlap]

        std_x = np.std(x)
        std_y = np.std(y)
        if std_x < 1e-12 or std_y < 1e-12:
            continue

        corr = float(np.corrcoef(x, y)[0, 1])
        if abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag

    return best_lag, best_corr


# ── Full Validation Pipeline ─────────────────────────────────────────────


def validate_relation(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    significance: float = 0.05,
) -> ValidationResult:
    """Run the full validation pipeline on a pair of price series.

    1. Estimate hedge ratio via OLS
    2. Compute spread
    3. Test spread stationarity (ADF)
    4. Test cointegration (Engle-Granger)
    5. Estimate half-life
    6. Compute correlation

    Returns a ValidationResult with all fields populated.
    """
    import numpy as np

    a = list(prices_a)
    b = list(prices_b)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    if n < 30:
        logger.warning('Validation: insufficient data (%d < 30 points)', n)
        return ValidationResult()

    # Hedge ratio
    hedge = estimate_hedge_ratio(a, b)

    # Spread
    spread = compute_spread(a, b, hedge_ratio=hedge)

    # ADF on spread
    adf_stat, adf_p, is_stationary = adf_test(spread, significance)

    # Cointegration
    coint_stat, coint_p, is_coint = engle_granger_test(a, b, significance)

    # Half-life
    hl = estimate_half_life(spread)

    # Correlation
    corr = pearson_correlation(a, b)

    # Lead-lag
    lag, lag_corr = detect_lead_lag(a, b)

    # Spread stats
    spread_arr = np.array(spread, dtype=float)
    mean_s = float(np.nanmean(spread_arr))
    std_s = float(np.nanstd(spread_arr))

    return ValidationResult(
        adf_statistic=adf_stat,
        adf_pvalue=adf_p,
        is_stationary=is_stationary,
        coint_statistic=coint_stat,
        coint_pvalue=coint_p,
        is_cointegrated=is_coint,
        half_life=hl if not math.isnan(hl) else None,
        hedge_ratio=hedge,
        correlation=corr,
        mean_spread=mean_s,
        std_spread=std_s,
        lead_lag=lag if lag != 0 else None,
        lead_lag_corr=lag_corr if lag != 0 else None,
    )
