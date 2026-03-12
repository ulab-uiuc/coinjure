from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from coinjure.ticker import CashTicker
from coinjure.trading.llm_sizing import (
    SizingContext,
    compute_llm_sizing,
    compute_opportunity_sizing_llm,
    OpportunitySizingRequest,
    get_opportunity_rate_limiter,
)
from coinjure.trading.sizing import compute_trade_size_with_llm


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


def _make_opportunity_request(
    *,
    strategy_id: str = 'test-strat',
    strategy_type: str = 'direct_arb',
    relation_type: str = 'same_event',
    edge: Decimal = Decimal('0.03'),
    available_capital: Decimal = Decimal('5000'),
    current_exposure: Decimal = Decimal('1200'),
    position_count: int = 3,
    portfolio_utilization: Decimal = Decimal('0.24'),
    quant_size: Decimal = Decimal('15'),
    kelly_fraction: Decimal = Decimal('0.1'),
    max_size: Decimal = Decimal('100'),
) -> OpportunitySizingRequest:
    """Create an OpportunitySizingRequest with sensible defaults for testing."""
    return OpportunitySizingRequest(
        strategy_id=strategy_id,
        strategy_type=strategy_type,
        relation_type=relation_type,
        edge=edge,
        available_capital=available_capital,
        current_exposure=current_exposure,
        position_count=position_count,
        portfolio_utilization=portfolio_utilization,
        quant_size=quant_size,
        kelly_fraction=kelly_fraction,
        max_size=max_size,
    )



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


def test_opportunity_sizing_valid_response():
    """Test that LLM returns valid size."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "20", "reasoning": "strong edge"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result == Decimal('20')


def test_opportunity_sizing_clamps_to_max():
    """Test that LLM result exceeding max_size is clamped."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "150", "reasoning": "too large"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request(max_size=Decimal('100'))

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result == Decimal('100')


def test_opportunity_sizing_rejects_zero_size():
    """Test that zero size is rejected."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "0", "reasoning": "no edge"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result is None


def test_opportunity_sizing_rejects_negative_size():
    """Test that negative size is rejected."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "-5", "reasoning": "invalid"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result is None


def test_opportunity_sizing_returns_none_on_api_error():
    """Test that API errors return None."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    fake_client, _ = _make_fake_client(side_effect=RuntimeError('api error'))
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result is None


def test_opportunity_sizing_rate_limited():
    """Test that rate limiter blocks call and returns None."""
    rate_limiter = get_opportunity_rate_limiter()
    original_interval = rate_limiter._min_interval

    try:
        rate_limiter.set_interval(10.0)
        rate_limiter._last_call = time.monotonic()

        response_content = '{"size": "20", "reasoning": "should not call"}'
        fake_client, create_mock = _make_fake_client(response_content)
        request = _make_opportunity_request()

        with patch(
            'coinjure.trading.llm_sizing._get_openai_client',
            return_value=fake_client,
        ):
            result = asyncio.run(compute_opportunity_sizing_llm(request))

        assert result is None
        create_mock.assert_not_called()
    finally:
        rate_limiter.set_interval(original_interval)
        rate_limiter._last_call = 0.0


def test_opportunity_sizing_rounds_to_integer():
    """Test that fractional sizes are quantized to integers."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "12.7", "reasoning": "fractional"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result == Decimal('13')


def test_opportunity_sizing_minimum_one():
    """Test that quantized result below 1 is clamped to 1."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "0.3", "reasoning": "tiny"}'
    fake_client, _ = _make_fake_client(response_content)
    request = _make_opportunity_request()

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(compute_opportunity_sizing_llm(request))

    assert result == Decimal('1')


def test_trade_size_with_llm_disabled():
    """Test that llm_trade_sizing=False returns quant size without calling LLM."""
    fake_client, create_mock = _make_fake_client('{"size": "25", "reasoning": "n/a"}')
    
    mock_pm = MagicMock()
    cash_pos = MagicMock()
    cash_pos.quantity = Decimal('5000')
    cash_pos.ticker = CashTicker(symbol='USD', name='US Dollar')
    mock_pm.get_cash_positions.return_value = [cash_pos]
    mock_pm.get_non_cash_positions.return_value = []

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(
            compute_trade_size_with_llm(
                mock_pm,
                Decimal('0.05'),
                strategy_id='test',
                strategy_type='direct_arb',
                relation_type='same_event',
                llm_trade_sizing=False,
            )
        )

    assert create_mock.call_count == 0
    assert result == Decimal('100')


def test_trade_size_with_llm_enabled_uses_llm_result():
    """Test that llm_trade_sizing=True uses the LLM size."""
    rate_limiter = get_opportunity_rate_limiter()
    rate_limiter._last_call = 0.0

    response_content = '{"size": "25", "reasoning": "strong edge"}'
    fake_client, _ = _make_fake_client(response_content)
    
    mock_pm = MagicMock()
    cash_pos = MagicMock()
    cash_pos.quantity = Decimal('5000')
    cash_pos.ticker = CashTicker(symbol='USD', name='US Dollar')
    mock_pm.get_cash_positions.return_value = [cash_pos]
    mock_pm.get_non_cash_positions.return_value = []

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(
            compute_trade_size_with_llm(
                mock_pm,
                Decimal('0.05'),
                strategy_id='test',
                strategy_type='direct_arb',
                relation_type='same_event',
                llm_trade_sizing=True,
            )
        )

    assert result == Decimal('25')


def test_trade_size_with_llm_falls_back_on_none():
    """Test that LLM returning None falls back to quant size."""
    fake_client, _ = _make_fake_client('{"size": "0", "reasoning": "rejected"}')
    
    mock_pm = MagicMock()
    cash_pos = MagicMock()
    cash_pos.quantity = Decimal('5000')
    cash_pos.ticker = CashTicker(symbol='USD', name='US Dollar')
    mock_pm.get_cash_positions.return_value = [cash_pos]
    mock_pm.get_non_cash_positions.return_value = []

    with patch(
        'coinjure.trading.llm_sizing._get_openai_client',
        return_value=fake_client,
    ):
        result = asyncio.run(
            compute_trade_size_with_llm(
                mock_pm,
                Decimal('0.05'),
                strategy_id='test',
                strategy_type='direct_arb',
                relation_type='same_event',
                llm_trade_sizing=True,
            )
        )

    assert result == Decimal('100')
