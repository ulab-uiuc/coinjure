from __future__ import annotations

import textwrap
from pathlib import Path

from click.testing import CliRunner

from coinjure.cli.cli import cli


def test_strategy_create_and_validate(tmp_path):
    runner = CliRunner()
    strategy_file = tmp_path / 'my_strategy.py'

    create = runner.invoke(
        cli,
        [
            'strategy',
            'create',
            '--output',
            str(strategy_file),
            '--class-name',
            'MyStrategy',
        ],
    )
    assert create.exit_code == 0
    assert strategy_file.exists()

    validate = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:MyStrategy',
            '--json',
        ],
    )
    assert validate.exit_code == 0
    assert '"ok": true' in validate.output


def test_backtest_run_invokes_runner(monkeypatch, tmp_path):
    # Create a dummy parquet file (content doesn't matter — run_backtest_parquet is mocked)
    parquet_file = tmp_path / 'orderbook.parquet'
    parquet_file.write_bytes(b'PAR1')

    captured = {}

    async def fake_run_backtest_parquet(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        'coinjure.engine.backtester.run_backtest_parquet',
        fake_run_backtest_parquet,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'backtest',
            '--parquet',
            str(parquet_file),
            '--market-id',
            'M1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured['parquet_path'] == str(parquet_file)
    assert captured['market_ids'] == ['M1']


def test_strategy_validate_with_kwargs_json(tmp_path):
    strategy_file = tmp_path / 'needs_kwargs.py'
    strategy_file.write_text(
        textwrap.dedent(
            """
            from coinjure.events import Event
            from coinjure.strategy.strategy import Strategy
            from coinjure.engine.trader.trader import Trader

            class NeedsKwargs(Strategy):
                def __init__(self, threshold: float):
                    self.threshold = threshold

                async def process_event(self, event: Event, trader: Trader) -> None:
                    return
            """
        ).strip()
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:NeedsKwargs',
            '--strategy-kwargs-json',
            '{"threshold": 0.7}',
            '--json',
        ],
    )

    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"threshold": 0.7' in result.output


def test_strategy_dry_run_with_kwargs_json(tmp_path):
    strategy_file = tmp_path / 'dry_run_strategy.py'
    strategy_file.write_text(
        textwrap.dedent(
            """
            from coinjure.events import Event
            from coinjure.strategy.strategy import Strategy
            from coinjure.engine.trader.trader import Trader

            class DryRunStrategy(Strategy):
                def __init__(self, multiplier: int):
                    self.multiplier = multiplier

                async def process_event(self, event: Event, trader: Trader) -> None:
                    return
            """
        ).strip()
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{strategy_file}:DryRunStrategy',
            '--strategy-kwargs-json',
            '{"multiplier": 2}',
            '--dry-run',
            '--events',
            '6',
            '--json',
        ],
    )

    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"events_processed": 6' in result.output


def test_example_strategy_files_validate() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    momentum = repo_root / 'examples' / 'strategies' / 'threshold_momentum_strategy.py'

    runner = CliRunner()
    result1 = runner.invoke(
        cli,
        [
            'strategy',
            'validate',
            '--strategy-ref',
            f'{momentum}:ThresholdMomentumStrategy',
            '--json',
        ],
    )

    assert result1.exit_code == 0


def test_paper_and_live_commands_invokable(monkeypatch):
    captured = {}

    class DummySource:
        def __init__(self, *args, **kwargs):
            captured['source_kwargs'] = kwargs

    async def fake_paper(**kwargs):
        captured['paper_kwargs'] = kwargs

    async def fake_live(**kwargs):
        captured['live_kwargs'] = kwargs

    monkeypatch.setattr(
        'coinjure.cli.agent_commands.LivePolyMarketDataSource', DummySource
    )
    monkeypatch.setattr('coinjure.engine.runner.run_live_paper_trading', fake_paper)
    monkeypatch.setattr('coinjure.engine.runner.run_live_polymarket_trading', fake_live)

    runner = CliRunner()
    paper_res = runner.invoke(
        cli,
        [
            'engine',
            'run',
            '--mode',
            'paper',
            '--exchange',
            'polymarket',
            '--duration',
            '1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
        ],
    )
    assert paper_res.exit_code == 0
    assert captured['paper_kwargs']['duration'] == 1.0

    live_res = runner.invoke(
        cli,
        [
            'engine',
            'run',
            '--mode',
            'live',
            '--exchange',
            'polymarket',
            '--wallet-private-key',
            'dummy',
            '--duration',
            '1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
        ],
        input='y\n',
    )
    assert live_res.exit_code == 0
    assert captured['live_kwargs']['duration'] == 1.0
