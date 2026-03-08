from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from coinjure.cli.cli import cli


def test_backtest_requires_relation_or_all(tmp_path):
    """engine backtest fails without --relation or --all-relations."""
    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'backtest'])
    assert result.exit_code != 0
    assert 'relation' in result.output.lower() or '--relation' in result.output


def test_backtest_relation_not_found(tmp_path, monkeypatch):
    """engine backtest fails for unknown relation ID."""
    from coinjure.market.relations import RelationStore as _RS

    empty_store = _RS(tmp_path / 'empty.json')
    monkeypatch.setattr(
        'coinjure.market.relations.RelationStore',
        lambda *a, **kw: empty_store,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'backtest', '--relation', 'nonexistent'])
    assert result.exit_code != 0
    assert 'not found' in result.output.lower()


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
        'coinjure.data.live.polymarket.LivePolyMarketDataSource',
        DummySource,
    )
    monkeypatch.setattr('coinjure.engine.runner.run_live_paper_trading', fake_paper)
    monkeypatch.setattr('coinjure.engine.runner.run_live_polymarket_trading', fake_live)

    runner = CliRunner()
    paper_res = runner.invoke(
        cli,
        [
            'engine',
            'paper-run',
            '--exchange',
            'polymarket',
            '--duration',
            '1',
            '--strategy-ref',
            'coinjure.strategy.demo:DemoStrategy',
        ],
    )
    assert paper_res.exit_code == 0
    assert captured['paper_kwargs']['duration'] == 1.0

    live_res = runner.invoke(
        cli,
        [
            'engine',
            'live-run',
            '--exchange',
            'polymarket',
            '--wallet-private-key',
            'dummy',
            '--duration',
            '1',
            '--strategy-ref',
            'coinjure.strategy.demo:DemoStrategy',
        ],
        input='y\n',
    )
    assert live_res.exit_code == 0
    assert captured['live_kwargs']['duration'] == 1.0
