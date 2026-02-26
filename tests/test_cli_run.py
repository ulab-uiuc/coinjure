from __future__ import annotations

from click.testing import CliRunner

from pm_cli.cli.cli import cli


def test_run_command_forwards_to_paper_run(monkeypatch):
    captured: dict = {}

    async def fake_run_live_paper_trading(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        'pm_cli.cli.agent_commands.run_live_paper_trading',
        fake_run_live_paper_trading,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ['run', '--exchange', 'rss', '--duration', '1'],
    )

    assert result.exit_code == 0
    assert 'Deprecated: use `pm-cli paper run` instead.' in result.output
    assert captured['continuous'] is True
    assert captured['duration'] == 1.0
