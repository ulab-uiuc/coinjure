from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from coinjure.trading.allocator import AllocationCandidate, allocate_capital

logger = logging.getLogger(__name__)

DEFAULT_MIN_BUDGET = Decimal('10')
DEFAULT_MAX_BUDGET_PCT = Decimal('0.4')
DEFAULT_RESERVE_PCT = Decimal('0.1')

DEFAULT_ALLOCATION_PROMPT = """You are a portfolio capital allocation reviewer for Coinjure.
You receive a baseline quantitative allocation and may adjust it.

Return one JSON object with this schema:
{
  "budgets": {
    "<strategy_id>": "<positive decimal budget>"
  },
  "reasoning": "brief explanation of the main adjustments"
}

Hard constraints:
- Use only the provided strategy IDs.
- Every budget must be strictly positive.
- The total budget must stay within deployable capital.
- No single strategy budget may exceed the per-strategy cap.
- Preserve reserve capital.
"""


class _ChatCompletionMessage(Protocol):
    content: str | None


class _ChatCompletionChoice(Protocol):
    message: _ChatCompletionMessage


class _ChatCompletionResponse(Protocol):
    choices: Sequence[_ChatCompletionChoice]


class _ChatCompletionsAPI(Protocol):
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, str],
    ) -> _ChatCompletionResponse: ...


class _ChatAPI(Protocol):
    completions: _ChatCompletionsAPI


class _OpenAIClient(Protocol):
    chat: _ChatAPI


# ---------------------------------------------------------------------------
# Singleton OpenAI client
# ---------------------------------------------------------------------------

_openai_client: _OpenAIClient | None = None


def _get_openai_client() -> _OpenAIClient:
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    try:
        import importlib

        module = importlib.import_module('openai')
    except ImportError as exc:
        raise RuntimeError(
            'openai is not installed. Install with `pip install openai`.'
        ) from exc
    factory = getattr(module, 'AsyncOpenAI', None)
    if factory is None or not callable(factory):
        raise RuntimeError('openai.AsyncOpenAI is not available in this environment.')
    _openai_client = cast(_OpenAIClient, factory())
    return _openai_client


def _render_allocation_input(
    total_capital: Decimal,
    candidates: list[AllocationCandidate],
    baseline: dict[str, Decimal],
    min_budget: Decimal,
    max_budget_pct: Decimal,
    reserve_pct: Decimal,
) -> str:
    deployable_capital = total_capital * (Decimal('1') - reserve_pct)
    per_strategy_cap = deployable_capital * max_budget_pct
    candidate_rows = [
        {'strategy_id': c.strategy_id, 'backtest_pnl': c.backtest_pnl}
        for c in candidates
    ]
    baseline_rows = {strategy_id: str(amount) for strategy_id, amount in baseline.items()}

    payload = {
        'total_capital': str(total_capital),
        'deployable_capital_limit': str(deployable_capital),
        'reserve_pct': str(reserve_pct),
        'min_budget': str(min_budget),
        'max_budget_pct': str(max_budget_pct),
        'per_strategy_cap': str(per_strategy_cap),
        'candidates': candidate_rows,
        'baseline_quant_allocation': baseline_rows,
        'task': (
            'Review the baseline allocation and return adjusted budgets plus '
            'reasoning while honoring all hard constraints.'
        ),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _append_audit_log(audit_file: str, record: dict[str, object]) -> None:
    """Append a single JSON record to a JSONL audit file."""
    try:
        with open(audit_file, 'a') as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + '\n')
    except OSError:
        logger.warning('Failed to write audit log to %s', audit_file, exc_info=True)


def _extract_response_content(response: _ChatCompletionResponse) -> str:
    if not response.choices:
        raise ValueError('LLM response contained no choices.')

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError('LLM response content is empty.')

    return content


def _parse_and_validate_budgets(
    content: str,
    *,
    expected_strategy_ids: set[str],
    deployable_capital: Decimal,
    max_per_strategy: Decimal,
) -> tuple[dict[str, Decimal], str]:
    parsed_obj = cast(object, json.loads(content))
    if not isinstance(parsed_obj, dict):
        raise ValueError('LLM payload must be a JSON object.')

    parsed_dict = cast(dict[object, object], parsed_obj)

    parsed: dict[str, object] = {}
    for key, value in parsed_dict.items():
        if not isinstance(key, str):
            raise ValueError('LLM payload keys must be strings.')
        parsed[key] = value

    raw_budgets_obj = parsed.get('budgets')
    if not isinstance(raw_budgets_obj, dict):
        raise ValueError('LLM payload missing `budgets` object.')

    raw_budgets_obj_typed = cast(dict[object, object], raw_budgets_obj)

    raw_budgets: dict[str, object] = {}
    for strategy_id, value in raw_budgets_obj_typed.items():
        if not isinstance(strategy_id, str):
            raise ValueError('Budget strategy IDs must be strings.')
        raw_budgets[strategy_id] = value

    llm_strategy_ids = set(raw_budgets)
    if llm_strategy_ids != expected_strategy_ids:
        raise ValueError('LLM budgets strategy IDs do not match candidates.')

    validated_budgets: dict[str, Decimal] = {}
    for strategy_id, raw_budget in raw_budgets.items():
        if isinstance(raw_budget, bool):
            raise ValueError(f'Budget for {strategy_id} must be numeric, not bool.')

        try:
            budget = Decimal(str(raw_budget))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f'Budget for {strategy_id} is not a valid decimal: {raw_budget!r}'
            ) from exc

        if not budget.is_finite():
            raise ValueError(f'Budget for {strategy_id} is not finite (got {budget!r}).')
        if budget <= 0:
            raise ValueError(f'Budget for {strategy_id} must be positive.')
        if budget > max_per_strategy:
            raise ValueError(
                f'Budget for {strategy_id} exceeds per-strategy cap {max_per_strategy}.'
            )

        validated_budgets[strategy_id] = budget

    total_allocated = sum(validated_budgets.values(), start=Decimal('0'))
    if total_allocated > deployable_capital:
        raise ValueError(
            f'Total budget {total_allocated} exceeds deployable capital {deployable_capital}.'
        )

    reasoning = parsed.get('reasoning', '')
    reasoning_text = reasoning.strip() if isinstance(reasoning, str) else ''
    return validated_budgets, reasoning_text


async def allocate_capital_llm(
    total_capital: Decimal,
    candidates: list[AllocationCandidate],
    *,
    model: str = 'gpt-4.1-mini',
    timeout: float = 30.0,
    min_budget: Decimal = DEFAULT_MIN_BUDGET,
    max_budget_pct: Decimal = DEFAULT_MAX_BUDGET_PCT,
    reserve_pct: Decimal = DEFAULT_RESERVE_PCT,
    audit_file: str = 'llm_allocation_audit.jsonl',
) -> dict[str, Decimal]:
    """Allocate strategy budgets with LLM review and strict fallback.

    This wrapper always computes the baseline quantitative allocation first using
    ``allocate_capital``. It then asks an LLM to review and optionally adjust the
    budgets. LLM output is accepted only when it passes all hard constraints.

    Args:
        total_capital: Total available capital to distribute.
        candidates: Strategy candidates with backtest results.
        model: OpenAI chat-completions model for allocation review.
        timeout: Timeout in seconds for the LLM API call.
        min_budget: Minimum capital per strategy used by the baseline allocator.
        max_budget_pct: Maximum share of total capital any strategy can receive.
        reserve_pct: Fraction of total capital held in reserve (not allocated).
        audit_file: Path to a JSONL file for logging LLM request/response pairs.

    Returns:
        Dict mapping strategy_id -> allocated capital (Decimal).

    Notes:
        This function is fail-safe: on any LLM/API/parsing/validation error,
        it returns the baseline ``allocate_capital`` result and never raises.
    """
    baseline = allocate_capital(
        total_capital,
        candidates,
        min_budget=min_budget,
        max_budget_pct=max_budget_pct,
        reserve_pct=reserve_pct,
    )
    if not candidates:
        return baseline

    deployable_capital = total_capital * (Decimal('1') - reserve_pct)
    max_per_strategy = deployable_capital * max_budget_pct
    expected_strategy_ids = {candidate.strategy_id for candidate in candidates}
    baseline_for_log = {
        strategy_id: str(amount) for strategy_id, amount in sorted(baseline.items())
    }

    request_summary = _render_allocation_input(
        total_capital,
        candidates,
        baseline,
        min_budget,
        max_budget_pct,
        reserve_pct,
    )

    try:
        client = _get_openai_client()
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': DEFAULT_ALLOCATION_PROMPT},
                    {
                        'role': 'user',
                        'content': request_summary,
                    },
                ],
                response_format={"type": "json_object"},
            ),
            timeout=timeout,
        )
        content = _extract_response_content(response)
        adjusted_budgets, reasoning = _parse_and_validate_budgets(
            content,
            expected_strategy_ids=expected_strategy_ids,
            deployable_capital=deployable_capital,
            max_per_strategy=max_per_strategy,
        )
    except Exception:
        logger.warning(
            'LLM allocation review failed; using baseline quant allocation. baseline=%s',
            baseline_for_log,
            exc_info=True,
        )
        return baseline

    adjusted_for_log = {
        strategy_id: str(amount)
        for strategy_id, amount in sorted(adjusted_budgets.items())
    }
    logger.info(
        'LLM allocation review accepted. baseline=%s adjusted=%s reasoning=%s',
        baseline_for_log,
        adjusted_for_log,
        reasoning or 'n/a',
    )

    # Audit trail
    _append_audit_log(audit_file, {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'request_summary': request_summary,
        'llm_response': content,
        'final_budgets': {sid: str(amt) for sid, amt in adjusted_budgets.items()},
    })

    return adjusted_budgets


@dataclass
class PortfolioAdjustment:
    kelly_fraction: Decimal | None = None
    max_trade_size: Decimal | None = None
    reasoning: str = ''


PORTFOLIO_REVIEW_PROMPT = """You are Coinjure's periodic portfolio reviewer.

You receive a snapshot of a single strategy's runtime state and decide whether
to adjust its sizing parameters.  Return ONLY valid JSON:

{
  "kelly_fraction": "<decimal or null to keep current>",
  "max_trade_size": "<decimal or null to keep current>",
  "reasoning": "one sentence"
}

Rules:
- kelly_fraction must be between 0.01 and 0.5 (or null).
- max_trade_size must be >= 1 (or null).
- If performance is healthy and exposure is reasonable, return nulls (no change).
- If drawdown is significant (> 10% of capital), reduce kelly_fraction.
- If utilization is very low (< 20%) and PnL is positive, consider increasing max_trade_size.
- Be conservative. Only adjust when the data clearly warrants it.
"""


def _render_portfolio_snapshot(
    *,
    strategy_id: str,
    available_capital: Decimal,
    current_exposure: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    position_count: int,
    kelly_fraction: Decimal,
    max_trade_size: Decimal,
    trade_count: int,
) -> str:
    total = available_capital + current_exposure
    utilization = current_exposure / total if total > 0 else Decimal('0')
    payload = {
        'strategy_id': strategy_id,
        'available_capital': str(available_capital),
        'current_exposure': str(current_exposure),
        'total_capital': str(total),
        'portfolio_utilization': str(utilization),
        'realized_pnl': str(realized_pnl),
        'unrealized_pnl': str(unrealized_pnl),
        'position_count': position_count,
        'trade_count': trade_count,
        'current_kelly_fraction': str(kelly_fraction),
        'current_max_trade_size': str(max_trade_size),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _parse_portfolio_adjustment(content: str) -> PortfolioAdjustment:
    parsed_obj = cast(object, json.loads(content))
    if not isinstance(parsed_obj, dict):
        raise ValueError('LLM payload must be a JSON object.')
    parsed = cast(dict[str, object], parsed_obj)

    adjustment = PortfolioAdjustment()

    raw_kelly = parsed.get('kelly_fraction')
    if raw_kelly is not None:
        try:
            kf = Decimal(str(raw_kelly))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f'kelly_fraction not valid: {raw_kelly!r}') from exc
        if not kf.is_finite():
            raise ValueError(f'kelly_fraction is not finite (got {kf!r}).')
        if kf < Decimal('0.01') or kf > Decimal('0.5'):
            raise ValueError(f'kelly_fraction {kf} out of range [0.01, 0.5]')
        adjustment.kelly_fraction = kf

    raw_max = parsed.get('max_trade_size')
    if raw_max is not None:
        try:
            ms = Decimal(str(raw_max))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f'max_trade_size not valid: {raw_max!r}') from exc
        if not ms.is_finite():
            raise ValueError(f'max_trade_size is not finite (got {ms!r}).')
        if ms < Decimal('1'):
            raise ValueError(f'max_trade_size {ms} must be >= 1')
        adjustment.max_trade_size = ms

    reasoning = parsed.get('reasoning', '')
    adjustment.reasoning = reasoning.strip() if isinstance(reasoning, str) else ''
    return adjustment


async def review_portfolio_llm(
    *,
    strategy_id: str,
    available_capital: Decimal,
    current_exposure: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    position_count: int,
    kelly_fraction: Decimal,
    max_trade_size: Decimal,
    trade_count: int = 0,
    model: str = 'gpt-4.1-mini',
    timeout: float = 15.0,
    audit_file: str = 'llm_allocation_audit.jsonl',
) -> PortfolioAdjustment | None:
    try:
        client = _get_openai_client()
        snapshot = _render_portfolio_snapshot(
            strategy_id=strategy_id,
            available_capital=available_capital,
            current_exposure=current_exposure,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            position_count=position_count,
            kelly_fraction=kelly_fraction,
            max_trade_size=max_trade_size,
            trade_count=trade_count,
        )
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': PORTFOLIO_REVIEW_PROMPT},
                    {'role': 'user', 'content': snapshot},
                ],
                response_format={'type': 'json_object'},
            ),
            timeout=timeout,
        )
        content = _extract_response_content(response)
        adjustment = _parse_portfolio_adjustment(content)

        # Audit trail for portfolio review
        _append_audit_log(audit_file, {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'type': 'portfolio_review',
            'strategy_id': strategy_id,
            'request_summary': snapshot,
            'llm_response': content,
            'adjustments': {
                'kelly_fraction': str(adjustment.kelly_fraction) if adjustment.kelly_fraction is not None else None,
                'max_trade_size': str(adjustment.max_trade_size) if adjustment.max_trade_size is not None else None,
                'reasoning': adjustment.reasoning,
            },
        })

        return adjustment
    except Exception:
        logger.warning(
            'LLM portfolio review failed for %s; no adjustments applied.',
            strategy_id,
            exc_info=True,
        )
        return None


__all__ = ['DEFAULT_ALLOCATION_PROMPT', 'allocate_capital_llm', 'review_portfolio_llm', 'PortfolioAdjustment']
