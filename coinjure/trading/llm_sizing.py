from __future__ import annotations

"""LLM-powered sizing advisor for launch-time strategy overrides.

Given strategy-level market state and portfolio-level constraints, ask an LLM for
conservative sizing parameter overrides and validate them before use.

Usage::

    overrides = await compute_llm_sizing(
        [
            SizingContext(
                strategy_id='rel-001',
                strategy_type='spread',
                relation_type='same_event',
                backtest_pnl=Decimal('12.5'),
                current_edge=Decimal('0.021'),
                volatility=Decimal('0.08'),
                total_capital=Decimal('10000'),
                allocated_budget=Decimal('1500'),
                current_exposure=Decimal('350'),
            )
        ]
    )
"""

import asyncio
import importlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

logger = logging.getLogger(__name__)

DEFAULT_SIZING_PROMPT = """You are Coinjure's launch-time sizing advisor.
Return ONLY valid JSON with a top-level object: {"overrides": [...] }.
Each override entry must contain:
- strategy_id: string
- kelly_fraction: positive number/string <= 0.5
- min_size: positive number/string
- max_size: positive number/string
- reasoning: short explanation grounded in the provided context
Rules:
- Use only provided context; no look-ahead or future assumptions.
- Be conservative when edge is weak or volatility/exposure is high.
- Do not invent strategy IDs; include only IDs from input contexts.
- Ensure min_size <= max_size.
"""


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
    ) -> '_ChatResponseLike': ...


class _ChatNamespaceLike(Protocol):
    completions: _ChatCompletionsLike


class _OpenAIClientLike(Protocol):
    chat: _ChatNamespaceLike


class _ChatResponseLike(Protocol):
    choices: Sequence[_ChatChoiceLike]


@dataclass
class SizingOverride:
    """Sizing override generated for one strategy.

    Attributes:
        kelly_fraction: Conservative Kelly multiplier (must be > 0 and <= 0.5).
        min_size: Per-trade floor in quote currency/contracts (must be > 0).
        max_size: Per-trade cap in quote currency/contracts (must be > 0).
        reasoning: Human-readable rationale emitted by the model.
    """

    kelly_fraction: Decimal
    min_size: Decimal
    max_size: Decimal
    reasoning: str


@dataclass
class SizingContext:
    """Context used by the sizing advisor for one strategy.

    Attributes:
        strategy_id: Stable strategy identifier.
        strategy_type: Strategy family/type label (e.g. ``spread``).
        relation_type: Relation taxonomy label (e.g. ``same_event``).
        backtest_pnl: Historical backtest PnL signal for the strategy.
        current_edge: Current estimated edge signal.
        volatility: Current market volatility estimate.
        total_capital: Total portfolio capital available.
        allocated_budget: Capital budget currently assigned to this strategy.
        current_exposure: Current open exposure for this strategy.
    """

    strategy_id: str
    strategy_type: str
    relation_type: str
    backtest_pnl: Decimal
    current_edge: Decimal
    volatility: Decimal
    total_capital: Decimal
    allocated_budget: Decimal
    current_exposure: Decimal


def _get_openai_client() -> _OpenAIClientLike:
    try:
        module = importlib.import_module('openai')
    except ImportError as exc:
        raise RuntimeError(
            'openai is not installed. Install with `pip install openai`.'
        ) from exc

    client_factory_obj = cast(object | None, getattr(module, 'AsyncOpenAI', None))
    if client_factory_obj is None or not callable(client_factory_obj):
        raise RuntimeError('openai.AsyncOpenAI is not available in this environment.')

    client = client_factory_obj()
    return cast(_OpenAIClientLike, client)


def _to_decimal(field: str, value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'{field} must be numeric, got bool')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be numeric, got {value!r}') from exc
    if not parsed.is_finite():
        raise ValueError(f'{field} must be finite, got {value!r}')
    return parsed


def _serialize_context(context: SizingContext) -> dict[str, str]:
    return {
        'strategy_id': context.strategy_id,
        'strategy_type': context.strategy_type,
        'relation_type': context.relation_type,
        'backtest_pnl': str(context.backtest_pnl),
        'current_edge': str(context.current_edge),
        'volatility': str(context.volatility),
        'total_capital': str(context.total_capital),
        'allocated_budget': str(context.allocated_budget),
        'current_exposure': str(context.current_exposure),
    }


def _extract_response_text(response: _ChatResponseLike) -> str:
    if not response.choices:
        raise ValueError('LLM response missing choices')

    content = response.choices[0].message.content
    if isinstance(content, str) and content.strip():
        return content
    raise ValueError('LLM response missing text content')


def _extract_rows(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError('LLM output must be a JSON object')

    payload_dict = cast(dict[str, object], payload)
    raw_overrides = payload_dict.get('overrides')
    if raw_overrides is None:
        raise ValueError('LLM output missing `overrides` field')

    if isinstance(raw_overrides, list):
        raw_overrides_list = cast(list[object], raw_overrides)
        rows: list[dict[str, object]] = []
        for row_obj in raw_overrides_list:
            if not isinstance(row_obj, dict):
                raise ValueError('Each override entry must be an object')
            rows.append(cast(dict[str, object], row_obj))
        return rows

    if isinstance(raw_overrides, dict):
        raw_overrides_dict = cast(dict[str, object], raw_overrides)
        rows = []
        for strategy_id_obj, row_obj in raw_overrides_dict.items():
            if not isinstance(row_obj, dict):
                raise ValueError('Override mapping must be {strategy_id: object}')
            copied = dict(cast(dict[str, object], row_obj))
            _ = copied.setdefault('strategy_id', strategy_id_obj)
            rows.append(copied)
        return rows

    raise ValueError('`overrides` must be a list or object')


def _validate_override(strategy_id: str, raw: dict[str, object]) -> SizingOverride | None:
    try:
        kelly_fraction = _to_decimal('kelly_fraction', raw.get('kelly_fraction'))
        min_size = _to_decimal('min_size', raw.get('min_size'))
        max_size = _to_decimal('max_size', raw.get('max_size'))
    except ValueError as exc:
        logger.warning('Discarding sizing override for %s: %s', strategy_id, exc)
        return None

    if kelly_fraction <= 0 or min_size <= 0 or max_size <= 0:
        logger.warning(
            'Discarding sizing override for %s: values must be positive', strategy_id
        )
        return None

    if kelly_fraction > Decimal('0.5'):
        logger.info(
            'Clamping kelly_fraction for %s from %s to 0.5', strategy_id, kelly_fraction
        )
        kelly_fraction = Decimal('0.5')

    if min_size > max_size:
        logger.warning(
            'Discarding sizing override for %s: min_size (%s) > max_size (%s)',
            strategy_id,
            min_size,
            max_size,
        )
        return None

    reasoning = str(raw.get('reasoning', '')).strip()
    if not reasoning:
        reasoning = 'LLM sizing override.'

    return SizingOverride(
        kelly_fraction=kelly_fraction,
        min_size=min_size,
        max_size=max_size,
        reasoning=reasoning,
    )


async def compute_llm_sizing(
    contexts: list[SizingContext],
    *,
    model: str = 'gpt-4.1-mini',
    timeout: float = 30.0,
) -> dict[str, SizingOverride]:
    """Compute launch-time sizing overrides with an LLM.

    The function asks an LLM for per-strategy overrides and validates the result
    before returning it. Any API/timeout/parsing failure falls back to an empty
    mapping so callers can continue with deterministic quant defaults.

    Args:
        contexts: Per-strategy sizing context used to request overrides.
        model: OpenAI chat model name.
        timeout: Maximum request time in seconds.

    Returns:
        Mapping of ``strategy_id`` to validated ``SizingOverride`` entries. Returns
        an empty dict when no valid overrides are available or on failure.
    """
    if not contexts:
        logger.info('Skipping LLM sizing: no contexts provided')
        return {}
    if timeout <= 0:
        logger.warning('Skipping LLM sizing: timeout must be positive, got %s', timeout)
        return {}

    known_strategy_ids = {ctx.strategy_id for ctx in contexts if ctx.strategy_id}
    if not known_strategy_ids:
        logger.warning('Skipping LLM sizing: no valid strategy IDs in contexts')
        return {}

    request_payload = {
        'contexts': [_serialize_context(context) for context in contexts],
        'output_schema': {
            'overrides': [
                {
                    'strategy_id': 'string',
                    'kelly_fraction': 'decimal string <= 0.5',
                    'min_size': 'positive decimal string',
                    'max_size': 'positive decimal string',
                    'reasoning': 'string',
                }
            ]
        },
    }

    logger.info(
        'Requesting LLM sizing overrides for %d strategies with model=%s',
        len(known_strategy_ids),
        model,
    )

    try:
        client = _get_openai_client()
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': DEFAULT_SIZING_PROMPT},
                    {
                        'role': 'user',
                        'content': json.dumps(request_payload, sort_keys=True),
                    },
                ],
                response_format={'type': 'json_object'},
            ),
            timeout=timeout,
        )
        content = _extract_response_text(response)
        payload_obj = cast(object, json.loads(content))
        rows = _extract_rows(payload_obj)
    except Exception:
        logger.exception('LLM sizing failed, falling back to quant defaults')
        return {}

    overrides: dict[str, SizingOverride] = {}
    for row in rows:
        strategy_id_raw = row.get('strategy_id')
        if not isinstance(strategy_id_raw, str) or not strategy_id_raw:
            logger.warning('Skipping override with missing strategy_id: %s', row)
            continue

        strategy_id = strategy_id_raw.strip()
        if strategy_id not in known_strategy_ids:
            logger.info('Skipping unknown strategy_id from LLM output: %s', strategy_id)
            continue

        override = _validate_override(strategy_id, row)
        if override is None:
            continue

        overrides[strategy_id] = override
        logger.info(
            'Accepted LLM sizing for %s: kelly=%s min=%s max=%s',
            strategy_id,
            override.kelly_fraction,
            override.min_size,
            override.max_size,
        )

    logger.info(
        'LLM sizing completed with %d valid overrides out of %d strategies',
        len(overrides),
        len(known_strategy_ids),
    )
    return overrides
