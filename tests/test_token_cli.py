"""Smoke tests for the token trading CLI and PaperTokenAdapter."""

from __future__ import annotations

from decimal import Decimal

from click.testing import CliRunner

from coinjure.cli.cli import cli
from coinjure.cli.token import PaperTokenAdapter
from coinjure.trader.types import TradeSide

# -- PaperTokenAdapter unit tests -------------------------------------------


class TestPaperTokenAdapter:
    def test_initial_positions_has_usdc(self) -> None:
        adapter = PaperTokenAdapter(initial_capital=Decimal('5000'))
        positions = adapter.get_positions()
        symbols = [p.ticker.symbol for p in positions]
        assert 'PolyMarket_USDC' in symbols

    def test_empty_orderbook(self) -> None:
        adapter = PaperTokenAdapter()
        ob = adapter.get_orderbook('nonexistent_token')
        assert ob.bids == []
        assert ob.asks == []

    def test_no_positions_for_unknown_token(self) -> None:
        adapter = PaperTokenAdapter()
        positions = adapter.get_positions(token_id='unknown')
        assert positions == []

    async def test_buy_no_liquidity_not_filled(self) -> None:
        adapter = PaperTokenAdapter(initial_capital=Decimal('10000'))
        result = await adapter.place_order(
            'test_token',
            TradeSide.BUY,
            Decimal('0.50'),
            Decimal('100'),
        )
        assert 'not filled' in result

    async def test_sell_without_position_rejected(self) -> None:
        adapter = PaperTokenAdapter()
        result = await adapter.place_order(
            'test_token',
            TradeSide.SELL,
            Decimal('0.50'),
            Decimal('100'),
        )
        assert 'REJECTED' in result

    async def test_buy_no_liquidity_no_position(self) -> None:
        adapter = PaperTokenAdapter(initial_capital=Decimal('10000'))
        await adapter.place_order(
            'tok1',
            TradeSide.BUY,
            Decimal('0.50'),
            Decimal('100'),
        )
        # No liquidity means no fill, so no position is created.
        positions = adapter.get_positions(token_id='tok1')
        assert positions == []


# -- CLI integration tests ---------------------------------------------------


class TestTokenCLI:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ['token', '--help'])
        assert result.exit_code == 0
        assert 'Token-level trading commands' in result.output

    def test_orderbook_empty(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ['token', 'orderbook', 'abc123'])
        assert result.exit_code == 0
        assert 'No order book data' in result.output

    def test_positions_empty(self) -> None:
        runner = CliRunner()
        # Need to reset module-level adapter for isolation
        import coinjure.cli.token as token_mod

        token_mod._adapter = PaperTokenAdapter(initial_capital=Decimal('0'))
        result = runner.invoke(cli, ['token', 'positions'])
        assert result.exit_code == 0
        token_mod._adapter = None  # reset

    def test_place_buy_no_liquidity(self) -> None:
        runner = CliRunner()
        import coinjure.cli.token as token_mod

        token_mod._adapter = None  # fresh adapter
        result = runner.invoke(
            cli,
            [
                'token',
                'place',
                '--token',
                'abc',
                '--side',
                'buy',
                '--price',
                '0.50',
                '--size',
                '10',
            ],
        )
        assert result.exit_code == 0
        assert 'not filled' in result.output
        token_mod._adapter = None

    def test_place_json_contract(self) -> None:
        runner = CliRunner()
        import coinjure.cli.token as token_mod

        token_mod._adapter = None
        result = runner.invoke(
            cli,
            [
                'token',
                'place',
                '--token',
                'abc',
                '--side',
                'buy',
                '--price',
                '0.50',
                '--size',
                '10',
                '--json',
            ],
        )

        assert result.exit_code == 0
        assert '"accepted"' in result.output
        assert '"executed"' in result.output
        assert '"failure_reason"' in result.output
        token_mod._adapter = None
