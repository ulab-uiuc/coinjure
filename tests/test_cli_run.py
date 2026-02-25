from __future__ import annotations

import json
from decimal import Decimal

from click.testing import CliRunner

from swm_agent.cli.cli import cli


def test_run_command_passes_continuous_and_drawdown(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.json'
    config_path.write_text(
        json.dumps(
            {
                'engine': {'initial_capital': 10000, 'continuous': False},
                'alerts': {'thresholds': {'drawdown_pct_alert': 0.15}},
                'storage': {'data_dir': str(tmp_path / 'data')},
            }
        )
    )

    captured: dict = {}

    async def fake_run_live_paper_trading(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        'swm_agent.live.live_trader.run_live_paper_trading',
        fake_run_live_paper_trading,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ['run', '--paper', '--duration', '1', '--config', str(config_path)],
    )

    assert result.exit_code == 0
    assert captured['continuous'] is False
    assert captured['drawdown_alert_pct'] == Decimal('0.15')
