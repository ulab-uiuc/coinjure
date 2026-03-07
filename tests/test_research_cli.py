"""Tests for strategy pipeline (formerly under research CLI group)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


def _write_iso_history(path: Path) -> None:
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'question': 'Market One',
            'volume': 1000,
            'time_series': {
                'Yes': [
                    {'t': f'2026-02-20T00:{i:02d}:00+00:00', 'p': 0.40 + i * 0.01}
                    for i in range(12)
                ]
            },
        },
        {
            'event_id': 'E2',
            'market_id': 'M2',
            'question': 'Market Two',
            'volume': 200,
            'time_series': {
                'Yes': [
                    {'t': f'2026-02-20T01:{i:02d}:00+00:00', 'p': 0.50 + i * 0.005}
                    for i in range(6)
                ]
            },
        },
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')


def test_research_alpha_pipeline(monkeypatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from coinjure.cli.cli import cli

    # Create a dummy parquet file (content doesn't matter — backtest is mocked)
    parquet_file = tmp_path / 'orderbook.parquet'
    parquet_file.write_bytes(b'PAR1')
    artifacts_dir = tmp_path / 'alpha_pipeline'

    def fake_dry_run(**kwargs):
        return {
            'ok': True,
            'events_requested': kwargs['dry_run_events'],
            'events_processed': kwargs['dry_run_events'],
            'orders_created': 1,
            'decision_stats': {},
            'error': None,
        }

    def fake_run_backtest_parquet_once(**kwargs):
        return {
            'total_trades': 8,
            'total_pnl': '12.0',
            'sharpe_ratio': '0.7',
            'win_rate': '0.55',
            'max_drawdown': '0.12',
        }

    monkeypatch.setattr(
        'coinjure.cli.research_helpers._run_strategy_dry_run', fake_dry_run
    )
    monkeypatch.setattr(
        'coinjure.cli.research_helpers._run_backtest_parquet_once',
        fake_run_backtest_parquet_once,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'strategy',
            'pipeline',
            '--parquet',
            str(parquet_file),
            '--strategy-ref',
            'dummy.module:Dummy',
            '--market-id',
            'M1',
            '--min-trades',
            '1',
            '--min-total-pnl',
            '0',
            '--max-drawdown-pct',
            '0.30',
            '--artifacts-dir',
            str(artifacts_dir),
            '--json',
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['passed'] is True
    assert (artifacts_dir / 'preflight.json').exists()
    assert (artifacts_dir / 'backtest_single.json').exists()
    assert (artifacts_dir / 'stress.jsonl').exists()
    assert (artifacts_dir / 'gate.json').exists()
