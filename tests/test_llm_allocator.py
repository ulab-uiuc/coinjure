from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from coinjure.trading.allocator import AllocationCandidate, allocate_capital
from coinjure.trading.llm_allocator import allocate_capital_llm


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


def test_allocate_capital_llm_accepts_valid_adjustments():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    response_content = (
        '{"budgets": {"alpha": "300", "beta": "200"}, "reasoning": "rebalance"}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == {'alpha': Decimal('300'), 'beta': Decimal('200')}
    assert result != baseline


def test_allocate_capital_llm_falls_back_on_malformed_json():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    fake_client, _ = _make_fake_client('not-json')

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == baseline


def test_allocate_capital_llm_falls_back_on_strategy_id_mismatch():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    response_content = (
        '{"budgets": {"alpha": "300", "gamma": "200"}, "reasoning": "mismatch"}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == baseline


def test_allocate_capital_llm_falls_back_when_total_exceeds_deployable():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
        AllocationCandidate(strategy_id='gamma', backtest_pnl=5.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    response_content = (
        '{"budgets": {"alpha": "350", "beta": "350", "gamma": "350"}, '
        '"reasoning": "too much total"}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == baseline


def test_allocate_capital_llm_falls_back_when_strategy_exceeds_cap():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    response_content = (
        '{"budgets": {"alpha": "401", "beta": "100"}, "reasoning": "too concentrated"}'
    )
    fake_client, _ = _make_fake_client(response_content)

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == baseline


def test_allocate_capital_llm_returns_empty_for_empty_candidates():
    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        side_effect=AssertionError('LLM client should not be called for empty candidates'),
    ):
        result = asyncio.run(allocate_capital_llm(Decimal('1000'), []))

    assert result == {}


def test_allocate_capital_llm_falls_back_on_api_exception():
    total_capital = Decimal('1000')
    candidates = [
        AllocationCandidate(strategy_id='alpha', backtest_pnl=90.0),
        AllocationCandidate(strategy_id='beta', backtest_pnl=10.0),
    ]
    baseline = allocate_capital(total_capital, candidates)
    fake_client, _ = _make_fake_client(side_effect=RuntimeError('api down'))

    with patch(
        'coinjure.trading.llm_allocator._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(allocate_capital_llm(total_capital, candidates))

    assert result == baseline
