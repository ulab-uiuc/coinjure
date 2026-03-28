"""LLM-powered per-opportunity sizing advisor.

Called inside the event loop when a strategy detects an arb opportunity.
The LLM receives real-time context (edge, capital, exposure, leg details)
and returns a trade size decision.

Usage::

    size = await compute_opportunity_sizing_llm(
        OpportunitySizingRequest(
            strategy_id='rel-001',
            strategy_type='direct_arb',
            relation_type='same_event',
            edge=Decimal('0.03'),
            available_capital=Decimal('5000'),
            current_exposure=Decimal('1200'),
            position_count=3,
            portfolio_utilization=Decimal('0.24'),
            quant_size=Decimal('15'),
            kelly_fraction=Decimal('0.1'),
            max_size=Decimal('100'),
            leg_count=2,
            leg_prices=[Decimal('0.55'), Decimal('0.52')],
        )
    )
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

logger = logging.getLogger(__name__)


class _ChatMessageLike(Protocol):
    content: str | None


class _ChatChoiceLike(Protocol):
    message: _ChatMessageLike


class _ChatCompletionsLike(Protocol):
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, str],
    ) -> _ChatResponseLike: ...


class _ChatNamespaceLike(Protocol):
    completions: _ChatCompletionsLike


class _OpenAIClientLike(Protocol):
    chat: _ChatNamespaceLike


class _ChatResponseLike(Protocol):
    choices: Sequence[_ChatChoiceLike]


# ---------------------------------------------------------------------------
# Singleton OpenAI client
# ---------------------------------------------------------------------------

_openai_client: _OpenAIClientLike | None = None


def _get_openai_client() -> _OpenAIClientLike:
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    try:
        module = importlib.import_module('openai')
    except ImportError as exc:
        raise RuntimeError(
            'openai is not installed. Install with `pip install openai`.'
        ) from exc

    client_factory_obj = cast(object | None, getattr(module, 'AsyncOpenAI', None))
    if client_factory_obj is None or not callable(client_factory_obj):
        raise RuntimeError('openai.AsyncOpenAI is not available in this environment.')

    _openai_client = cast(_OpenAIClientLike, client_factory_obj())
    return _openai_client


def _to_decimal(field_name: str, value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'{field_name} must be numeric, got bool')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{field_name} must be numeric, got {value!r}') from exc
    if not parsed.is_finite():
        raise ValueError(f'{field_name} must be finite, got {value!r}')
    return parsed


def _extract_response_text(response: _ChatResponseLike) -> str:
    if not response.choices:
        raise ValueError('LLM response missing choices')

    content = response.choices[0].message.content
    if isinstance(content, str) and content.strip():
        return content
    raise ValueError('LLM response missing text content')


OPPORTUNITY_SIZING_PROMPT = """You are Coinjure's per-opportunity sizing advisor.

A trading strategy has detected an arbitrage opportunity and is asking you
to decide the trade size (per-leg quantity in contracts).  Return ONLY valid JSON:

{
  "size": "<positive integer — number of contracts PER LEG>",
  "reasoning": "<one sentence explaining the decision>"
}

Decision factors you MUST consider:
- edge: how large the arbitrage edge is (higher edge → larger size OK)
- available_capital: cash available — the total cost across all legs must not exceed this
- current_exposure: how much capital is already at risk (high → be conservative)
- portfolio_utilization: fraction of total portfolio already deployed (>0.6 → small)
- quant_size: the baseline quant model's suggested size (use as anchor)
- max_size: absolute ceiling — never exceed this
- kelly_fraction: the Kelly multiplier in use
- leg_count: number of legs in this trade — total capital required = size × sum(leg_prices)
- leg_prices: price per contract for each leg

Rules:
- Return an integer >= 1 and <= max_size.
- The returned size is used for EVERY leg. Total cost ≈ size × sum(leg_prices).
  Ensure size × sum(leg_prices) does not exceed available_capital.
- If edge is thin (<= 0.01) or exposure is high (>60% utilization), size conservatively (near or below quant_size).
- If edge is strong (> 0.03) and exposure is low, you may size above quant_size up to max_size.
- Never return 0. Minimum is 1.
- Do not invent data. Use only what is provided.
"""


@dataclass
class OpportunitySizingRequest:

    strategy_id: str
    strategy_type: str
    relation_type: str
    edge: Decimal
    available_capital: Decimal
    current_exposure: Decimal
    position_count: int
    portfolio_utilization: Decimal
    quant_size: Decimal
    kelly_fraction: Decimal
    max_size: Decimal
    leg_count: int = 1
    leg_prices: list[Decimal] = field(default_factory=list)


def _serialize_opportunity(req: OpportunitySizingRequest) -> dict[str, object]:
    return {
        'strategy_id': req.strategy_id,
        'strategy_type': req.strategy_type,
        'relation_type': req.relation_type,
        'edge': str(req.edge),
        'available_capital': str(req.available_capital),
        'current_exposure': str(req.current_exposure),
        'position_count': str(req.position_count),
        'portfolio_utilization': str(req.portfolio_utilization),
        'quant_size': str(req.quant_size),
        'kelly_fraction': str(req.kelly_fraction),
        'max_size': str(req.max_size),
        'leg_count': req.leg_count,
        'leg_prices': [str(p) for p in req.leg_prices],
    }


_OPPORTUNITY_MIN_INTERVAL_SECONDS = 0.0


class _RateLimiter:

    def __init__(
        self,
        min_interval_seconds: float = _OPPORTUNITY_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._min_interval: float = min_interval_seconds
        self._last_call: float = 0.0

    def should_skip(self) -> bool:
        now = time.monotonic()
        if now - self._last_call < self._min_interval:
            return True
        self._last_call = now
        return False

    def set_interval(self, seconds: float) -> None:
        self._min_interval = max(0.0, seconds)


_opportunity_rate_limiter = _RateLimiter(
    min_interval_seconds=_OPPORTUNITY_MIN_INTERVAL_SECONDS,
)


def get_opportunity_rate_limiter() -> _RateLimiter:
    return _opportunity_rate_limiter


async def compute_opportunity_sizing_llm(
    request: OpportunitySizingRequest,
    *,
    model: str = 'gpt-4.1-mini',
    timeout: float = 15.0,
) -> Decimal | None:
    if _opportunity_rate_limiter.should_skip():
        logger.debug(
            'Per-opportunity LLM sizing skipped (rate-limited) for %s',
            request.strategy_id,
        )
        return None

    payload = {
        'opportunity': _serialize_opportunity(request),
    }

    logger.info(
        'Per-opportunity LLM sizing for %s: edge=%s exposure=%s quant_size=%s',
        request.strategy_id,
        request.edge,
        request.current_exposure,
        request.quant_size,
    )

    try:
        client = _get_openai_client()
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': OPPORTUNITY_SIZING_PROMPT},
                    {
                        'role': 'user',
                        'content': json.dumps(payload, sort_keys=True),
                    },
                ],
                response_format={'type': 'json_object'},
            ),
            timeout=timeout,
        )
        content = _extract_response_text(response)
        parsed = cast(object, json.loads(content))
    except Exception:
        logger.exception(
            'Per-opportunity LLM sizing failed for %s, falling back to quant',
            request.strategy_id,
        )
        return None

    if not isinstance(parsed, dict):
        logger.warning('Per-opportunity LLM output is not a JSON object')
        return None

    parsed_dict = cast(dict[str, object], parsed)
    raw_size = parsed_dict.get('size')
    reasoning = str(parsed_dict.get('reasoning', '')).strip()

    try:
        size = _to_decimal('size', raw_size)
    except ValueError as exc:
        logger.warning('Per-opportunity LLM returned invalid size: %s', exc)
        return None

    if size <= 0:
        logger.warning('Per-opportunity LLM returned non-positive size: %s', size)
        return None

    if size > request.max_size:
        logger.info(
            'Per-opportunity LLM size %s clamped to max_size %s',
            size,
            request.max_size,
        )
        size = request.max_size

    # Enforce capital constraint: size × sum(leg_prices) <= available_capital
    total_leg_price = Decimal('0')
    for p in request.leg_prices:
        if isinstance(p, Decimal) and p > 0:
            total_leg_price += p
    if total_leg_price > 0:
        try:
            max_by_capital = request.available_capital / total_leg_price
        except (InvalidOperation, ZeroDivisionError):
            max_by_capital = Decimal('0')
        if max_by_capital <= 0:
            logger.info(
                'Per-opportunity LLM sizing for %s: insufficient capital '
                '(available=%s, total_leg_price=%s)',
                request.strategy_id,
                request.available_capital,
                total_leg_price,
            )
            return None
        if size > max_by_capital:
            logger.info(
                'Per-opportunity LLM size %s clamped to capital-constrained max %s '
                '(available=%s, total_leg_price=%s)',
                size,
                max_by_capital,
                request.available_capital,
                total_leg_price,
            )
            size = max_by_capital

    size = size.quantize(Decimal('1'))
    if size <= 0:
        logger.info(
            'Per-opportunity LLM sizing for %s: size rounded to non-positive after '
            'constraints; no trade',
            request.strategy_id,
        )
        return None

    logger.info(
        'Per-opportunity LLM sizing for %s: size=%s (quant=%s) reason=%s',
        request.strategy_id,
        size,
        request.quant_size,
        reasoning or 'n/a',
    )
    return size
