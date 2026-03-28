from __future__ import annotations

from decimal import Decimal
from typing import Optional

from coinjure.ticker import Ticker
from coinjure.trading.llm_sizing import (
    OpportunitySizingRequest,
    compute_opportunity_sizing_llm,
)
from coinjure.trading.position import PositionManager

# Non-blocking LLM sizing state: caches last LLM result per strategy,
# and tracks which strategies have a pending LLM call.
_llm_size_cache: dict[str, Decimal] = {}
_llm_pending: set[str] = set()


def _dynamic_kelly(
    edge: Decimal,
    win_rate: float,
    static_fraction: Decimal,
) -> Decimal:
    """Compute a conservative Kelly fraction from win rate and edge.

    For prediction markets the average payoff is ``edge / (1 - edge)``.
    The optimal Kelly fraction is then::

        kelly = (win_rate * avg_payoff - (1 - win_rate)) / avg_payoff

    We take the minimum of the computed Kelly and the static fraction to
    stay conservative.  If the computed value is non-positive (no edge),
    the static fraction is returned unchanged.
    """
    if edge >= Decimal('1'):
        return static_fraction
    avg_payoff = float(edge) / (1.0 - float(edge))
    if avg_payoff <= 0:
        return static_fraction
    computed = (win_rate * avg_payoff - (1.0 - win_rate)) / avg_payoff
    if computed <= 0:
        return static_fraction
    return min(Decimal(str(computed)), static_fraction)


def compute_trade_size(
    position_manager: PositionManager,
    edge: Decimal,
    *,
    collateral: Ticker | None = None,
    kelly_fraction: Decimal = Decimal('0.1'),
    edge_cap: Decimal = Decimal('0.10'),
    min_size: Decimal = Decimal('1'),
    max_size: Decimal = Decimal('100'),
    win_rate: Optional[float] = None,
) -> Decimal:
    """Compute trade size weighted by edge relative to available capital.

    When ``win_rate`` is provided, the effective Kelly fraction is computed
    dynamically as the minimum of the optimal Kelly (derived from win_rate
    and edge) and the static ``kelly_fraction``.

    Formula::

        available = sum(cash positions matching collateral)
        weight    = min(edge / edge_cap, 1)
        raw       = available * effective_kelly * weight
        result    = clamp(raw, min_size, max_size)
    """
    available = Decimal('0')
    for pos in position_manager.get_cash_positions():
        if collateral is not None and pos.ticker != collateral:
            continue
        available += pos.quantity

    if available <= 0 or edge <= 0:
        return min_size

    effective_kelly = kelly_fraction
    if win_rate is not None:
        effective_kelly = _dynamic_kelly(edge, win_rate, kelly_fraction)

    weight = min(edge / edge_cap, Decimal('1'))
    raw = available * effective_kelly * weight

    if raw < min_size:
        return min_size
    if raw > max_size:
        return max_size
    return raw.quantize(Decimal('1'))


def _available_capital(position_manager: PositionManager) -> Decimal:
    available = Decimal('0')
    for pos in position_manager.get_cash_positions():
        available += pos.quantity
    return available


def _current_exposure(position_manager: PositionManager) -> Decimal:
    exposure = Decimal('0')
    for pos in position_manager.get_non_cash_positions():
        if pos.quantity > 0:
            exposure += pos.quantity * pos.average_cost
    return exposure


async def compute_trade_size_with_llm(
    position_manager: PositionManager,
    edge: Decimal,
    *,
    strategy_id: str,
    strategy_type: str,
    relation_type: str,
    llm_trade_sizing: bool = False,
    llm_model: str | None = None,
    kelly_fraction: Decimal = Decimal('0.1'),
    edge_cap: Decimal = Decimal('0.10'),
    min_size: Decimal = Decimal('1'),
    max_size: Decimal = Decimal('100'),
    leg_count: int = 1,
    leg_prices: list[Decimal] | None = None,
    **kwargs: object,
) -> Decimal:
    import asyncio
    import logging

    _logger = logging.getLogger(__name__)

    quant_size = compute_trade_size(
        position_manager,
        edge,
        kelly_fraction=kelly_fraction,
        edge_cap=edge_cap,
        min_size=min_size,
        max_size=max_size,
    )
    if not llm_trade_sizing:
        return quant_size

    # Return cached LLM size if available
    if strategy_id in _llm_size_cache:
        cached = _llm_size_cache[strategy_id]
        return max(min_size, min(cached, max_size))

    # If an LLM call is already in flight, return quant size immediately
    if strategy_id in _llm_pending:
        return quant_size

    non_cash = position_manager.get_non_cash_positions()
    available = _available_capital(position_manager)
    exposure = Decimal('0')
    for pos in non_cash:
        if pos.quantity > 0:
            exposure += pos.quantity * pos.average_cost
    total = available + exposure
    utilization = exposure / total if total > 0 else Decimal('0')
    request = OpportunitySizingRequest(
        strategy_id=strategy_id,
        strategy_type=strategy_type,
        relation_type=relation_type,
        edge=edge,
        available_capital=available,
        current_exposure=exposure,
        position_count=len(non_cash),
        portfolio_utilization=utilization,
        quant_size=quant_size,
        kelly_fraction=kelly_fraction,
        max_size=max_size,
        leg_count=leg_count,
        leg_prices=leg_prices or [],
    )

    _llm_coro = compute_opportunity_sizing_llm(
        request,
        model=llm_model or 'gpt-4.1-mini',
        timeout=60.0,
    )

    # Try to get LLM result within a short window. If the LLM responds
    # quickly we use it directly; otherwise fire a background task and
    # return the quant size so we never block the event loop.
    _NON_BLOCKING_TIMEOUT = 0.3
    try:
        llm_size = await asyncio.wait_for(_llm_coro, timeout=_NON_BLOCKING_TIMEOUT)
    except (asyncio.TimeoutError, Exception) as exc:
        if isinstance(exc, asyncio.TimeoutError):
            # LLM too slow — schedule background task for caching
            async def _background_llm() -> None:
                try:
                    result = await compute_opportunity_sizing_llm(
                        request,
                        model=llm_model or 'gpt-4.1-mini',
                        timeout=60.0,
                    )
                    if result is not None:
                        _llm_size_cache[strategy_id] = result
                except Exception as bg_exc:
                    _logger.debug('Background LLM sizing failed: %s', bg_exc)
                finally:
                    _llm_pending.discard(strategy_id)

            _llm_pending.add(strategy_id)
            asyncio.create_task(_background_llm())
        else:
            _logger.debug('LLM sizing error: %s', exc)
        return quant_size

    if llm_size is None:
        return quant_size
    if llm_size < min_size:
        return min_size
    if llm_size > max_size:
        return max_size
    return llm_size
