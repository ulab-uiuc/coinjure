from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from coinjure.trading.llm_sizing import SizingContext, compute_llm_sizing


def _make_fake_client(
    content: str | None = None,
    *,
    side_effect: Exception | None = None,
) -> tuple[object, AsyncMock]:
    class FakeMessage:
        content: str

        def __init__(self, value: str):
            self.content = value

    class FakeChoice:
        message: FakeMessage

        def __init__(self, value: str):
            self.message = FakeMessage(value)

    class FakeResponse:
        choices: list[FakeChoice]

        def __init__(self, value: str):
            self.choices = [FakeChoice(value)]

    create_mock = AsyncMock()
    if side_effect is not None:
        create_mock.side_effect = side_effect
    else:
        create_mock.return_value = FakeResponse(content or '')

    class FakeCompletions:
        create: AsyncMock

        def __init__(self, create: AsyncMock):
            self.create = create

    class FakeChat:
        completions: FakeCompletions

        def __init__(self, create: AsyncMock):
            self.completions = FakeCompletions(create)

    class FakeClient:
        chat: FakeChat

        def __init__(self, create: AsyncMock):
            self.chat = FakeChat(create)

    return FakeClient(create_mock), create_mock


def _make_contexts() -> list[SizingContext]:
    return [
        SizingContext(
            strategy_id='alpha',
            strategy_type='spread',
            relation_type='same_event',
            backtest_pnl=Decimal('12.5'),
            current_edge=Decimal('0.02'),
            volatility=Decimal('0.08'),
            total_capital=Decimal('10000'),
            allocated_budget=Decimal('1500'),
            current_exposure=Decimal('350'),
        ),
        SizingContext(
            strategy_id='beta',
            strategy_type='spread',
            relation_type='cross_market',
            backtest_pnl=Decimal('7.5'),
            current_edge=Decimal('0.015'),
            volatility=Decimal('0.10'),
            total_capital=Decimal('10000'),
            allocated_budget=Decimal('1000'),
            current_exposure=Decimal('250'),
        ),
    ]


def test_compute_llm_sizing_accepts_valid_overrides():
    contexts = _make_contexts()
    response_content = (
        '{"overrides": ['
        '{"strategy_id": "alpha", "kelly_fraction": "0.20", "min_size": "5", '
        '"max_size": "25", "reasoning": "strong edge"}, '
        '{"strategy_id": "beta", "kelly_fraction": "0.10", "min_size": "3", '
        '"max_size": "15", "reasoning": "moderate edge"}'
        ']}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert set(result) == {'alpha', 'beta'}
    assert result['alpha'].kelly_fraction == Decimal('0.20')
    assert result['alpha'].min_size == Decimal('5')
    assert result['alpha'].max_size == Decimal('25')
    assert result['beta'].kelly_fraction == Decimal('0.10')
    assert result['beta'].min_size == Decimal('3')
    assert result['beta'].max_size == Decimal('15')


def test_compute_llm_sizing_clamps_kelly_fraction_to_half():
    contexts = _make_contexts()
    response_content = (
        '{"overrides": ['
        '{"strategy_id": "alpha", "kelly_fraction": "0.9", "min_size": "5", '
        '"max_size": "25", "reasoning": "aggressive"}'
        ']}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert result['alpha'].kelly_fraction == Decimal('0.5')
    assert result['alpha'].min_size == Decimal('5')
    assert result['alpha'].max_size == Decimal('25')


def test_compute_llm_sizing_discards_only_override_with_min_gt_max():
    contexts = _make_contexts()
    response_content = (
        '{"overrides": ['
        '{"strategy_id": "alpha", "kelly_fraction": "0.20", "min_size": "30", '
        '"max_size": "10", "reasoning": "invalid band"}, '
        '{"strategy_id": "beta", "kelly_fraction": "0.15", "min_size": "4", '
        '"max_size": "12", "reasoning": "valid band"}'
        ']}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert 'alpha' not in result
    assert 'beta' in result
    assert result['beta'].kelly_fraction == Decimal('0.15')


def test_compute_llm_sizing_discards_negative_values():
    contexts = _make_contexts()
    response_content = (
        '{"overrides": ['
        '{"strategy_id": "alpha", "kelly_fraction": "-0.1", "min_size": "5", '
        '"max_size": "25", "reasoning": "invalid sign"}'
        ']}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert result == {}


def test_compute_llm_sizing_skips_unknown_strategy_ids():
    contexts = _make_contexts()
    response_content = (
        '{"overrides": ['
        '{"strategy_id": "unknown", "kelly_fraction": "0.2", "min_size": "5", '
        '"max_size": "25", "reasoning": "skip me"}, '
        '{"strategy_id": "alpha", "kelly_fraction": "0.1", "min_size": "2", '
        '"max_size": "10", "reasoning": "known id"}'
        ']}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert set(result) == {'alpha'}
    assert result['alpha'].kelly_fraction == Decimal('0.1')


def test_compute_llm_sizing_returns_empty_for_empty_contexts():
    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        side_effect=AssertionError('LLM client should not be called for empty contexts'),
    ):
        result = asyncio.run(compute_llm_sizing([]))

    assert result == {}


def test_compute_llm_sizing_returns_empty_on_api_exception():
    contexts = _make_contexts()
    fake_client, _ = _make_fake_client(side_effect=RuntimeError('api down'))

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_llm_sizing(contexts))

    assert result == {}
