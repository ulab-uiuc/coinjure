from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from click.testing import CliRunner

from coinjure.cli.cli import cli


def _write_history(path: Path) -> None:
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 1, 'p': 0.40}, {'t': 2, 'p': 0.45}]},
        },
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 3, 'p': 0.55}, {'t': 4, 'p': 0.50}]},
        },
        {
            'event_id': 'E2',
            'market_id': 'M2',
            'time_series': {'Yes': [{'t': 1, 'p': 0.90}]},
        },
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


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


def test_research_group_help_lists_tools() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ['research', '--help'])
    assert result.exit_code == 0
    assert 'strategy-discovery tooling' in result.output
    assert 'slice' in result.output
    assert 'features' in result.output
    assert 'labels' in result.output
    assert 'backtest-batch' in result.output
    assert 'walk-forward' in result.output
    assert 'stress-test' in result.output
    assert 'compare-runs' in result.output
    assert 'strategy-gate' in result.output
    assert 'markets' in result.output
    assert 'walk-forward-auto' in result.output
    assert 'alpha-pipeline' in result.output
    assert 'memory' in result.output


def test_research_slice_features_labels(tmp_path: Path) -> None:
    history = tmp_path / 'history.jsonl'
    _write_history(history)
    sliced = tmp_path / 'slice.jsonl'
    features = tmp_path / 'features.jsonl'
    labels = tmp_path / 'labels.jsonl'

    runner = CliRunner()
    slice_result = runner.invoke(
        cli,
        [
            'research',
            'slice',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--start-ts',
            '2',
            '--end-ts',
            '4',
            '--output',
            str(sliced),
            '--json',
        ],
    )
    assert slice_result.exit_code == 0
    assert '"points": 3' in slice_result.output

    feature_result = runner.invoke(
        cli,
        [
            'research',
            'features',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--windows',
            '2,3',
            '--z-window',
            '3',
            '--output',
            str(features),
            '--json',
        ],
    )
    assert feature_result.exit_code == 0
    feature_rows = _load_jsonl(features)
    assert len(feature_rows) == 4
    assert 'momentum_2' in feature_rows[0]
    assert 'zscore_3' in feature_rows[-1]

    label_result = runner.invoke(
        cli,
        [
            'research',
            'labels',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--horizon-steps',
            '1',
            '--threshold',
            '0.01',
            '--output',
            str(labels),
            '--json',
        ],
    )
    assert label_result.exit_code == 0
    label_rows = _load_jsonl(labels)
    assert len(label_rows) == 3
    assert 'label_up' in label_rows[0]


def test_research_backtest_batch_writes_output(monkeypatch, tmp_path: Path) -> None:
    history = tmp_path / 'history.jsonl'
    _write_history(history)
    params = tmp_path / 'params.jsonl'
    params_rows = [
        {'id': 'r1', 'strategy_kwargs': {'trade_size': 10}},
        {'id': 'r2', 'entry_z': 1.2},
    ]
    params.write_text(
        '\n'.join(json.dumps(row) for row in params_rows) + '\n',
        encoding='utf-8',
    )
    output = tmp_path / 'batch_out.jsonl'

    def fake_run_backtest_once(**kwargs):
        return {
            'total_trades': 2,
            'total_pnl': str(Decimal('12.5')),
            'sharpe_ratio': '1.0',
            'win_rate': '0.5',
            'max_drawdown': '0.1',
            'received_kwargs': kwargs['strategy_kwargs'],
        }

    monkeypatch.setattr(
        'coinjure.cli.research_commands._run_backtest_once', fake_run_backtest_once
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'research',
            'backtest-batch',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
            '--params-jsonl',
            str(params),
            '--output',
            str(output),
            '--json',
        ],
    )
    assert result.exit_code == 0
    out_rows = _load_jsonl(output)
    assert len(out_rows) == 2
    assert out_rows[0]['ok'] is True
    assert out_rows[1]['metrics']['received_kwargs'] == {'entry_z': 1.2}


def test_research_compare_runs_and_memory(tmp_path: Path) -> None:
    runs_file = tmp_path / 'runs.jsonl'
    runs_rows = [
        {'name': 'low', 'ok': True, 'metrics': {'total_pnl': '1.0', 'win_rate': '0.5'}},
        {
            'name': 'high',
            'ok': True,
            'metrics': {'total_pnl': '8.0', 'win_rate': '0.6'},
        },
    ]
    runs_file.write_text(
        '\n'.join(json.dumps(row) for row in runs_rows) + '\n',
        encoding='utf-8',
    )
    ranked_file = tmp_path / 'ranked.jsonl'
    memory_file = tmp_path / 'run_memory.jsonl'

    runner = CliRunner()
    compare = runner.invoke(
        cli,
        [
            'research',
            'compare-runs',
            '--input-file',
            str(runs_file),
            '--sort-key',
            'total_pnl',
            '--top',
            '1',
            '--output',
            str(ranked_file),
            '--json',
        ],
    )
    assert compare.exit_code == 0
    ranked_rows = _load_jsonl(ranked_file)
    assert len(ranked_rows) == 1
    assert ranked_rows[0]['name'] == 'high'

    add = runner.invoke(
        cli,
        [
            'research',
            'memory',
            'add',
            '--input-file',
            str(ranked_file),
            '--memory-file',
            str(memory_file),
            '--tag',
            'exp-a',
            '--json',
        ],
    )
    assert add.exit_code == 0

    list_res = runner.invoke(
        cli,
        [
            'research',
            'memory',
            'list',
            '--memory-file',
            str(memory_file),
            '--tag',
            'exp-a',
            '--json',
        ],
    )
    assert list_res.exit_code == 0
    payload = json.loads(list_res.output)
    assert payload['count'] == 1


def test_research_markets_and_walk_forward_auto(monkeypatch, tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    _write_iso_history(history)
    wf_output = tmp_path / 'wf_auto.jsonl'

    def fake_run_backtest_once(**kwargs):
        return {
            'total_trades': 5,
            'total_pnl': '10.0',
            'sharpe_ratio': '0.8',
            'win_rate': '0.6',
            'max_drawdown': '0.1',
            'kwargs': kwargs['strategy_kwargs'],
        }

    monkeypatch.setattr(
        'coinjure.cli.research_commands._run_backtest_once', fake_run_backtest_once
    )

    runner = CliRunner()
    markets = runner.invoke(
        cli,
        [
            'research',
            'markets',
            '--history-file',
            str(history),
            '--sort-by',
            'points',
            '--limit',
            '1',
            '--json',
        ],
    )
    assert markets.exit_code == 0
    payload = json.loads(markets.output)
    assert payload['count'] == 1
    assert payload['markets'][0]['market_id'] == 'M1'
    assert payload['markets'][0]['points'] == 12

    wf = runner.invoke(
        cli,
        [
            'research',
            'walk-forward-auto',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
            '--min-train-size',
            '20',
            '--min-test-size',
            '10',
            '--target-runs',
            '2',
            '--output',
            str(wf_output),
            '--json',
        ],
    )
    assert wf.exit_code == 0
    wf_payload = json.loads(wf.output)
    assert wf_payload['n_points'] == 12
    # Auto sizing should shrink requested windows to fit the available points.
    assert wf_payload['train_size'] < 20
    assert wf_payload['test_size'] < 10
    assert wf_payload['auto_resized'] is True
    assert wf_payload['runs'] >= 1
    rows = _load_jsonl(wf_output)
    assert len(rows) == wf_payload['runs']


def test_research_walk_forward_auto_resizes(monkeypatch, tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    _write_iso_history(history)
    output = tmp_path / 'wf_manual.jsonl'

    def fake_run_backtest_once(**kwargs):
        return {
            'total_trades': 2,
            'total_pnl': '1.0',
            'sharpe_ratio': '0.2',
            'win_rate': '0.5',
            'max_drawdown': '0.1',
        }

    monkeypatch.setattr(
        'coinjure.cli.research_commands._run_backtest_once', fake_run_backtest_once
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'research',
            'walk-forward',
            '--history-file',
            str(history),
            '--market-id',
            'M1',
            '--event-id',
            'E1',
            '--strategy-ref',
            'coinjure.strategy.test_strategy:TestStrategy',
            '--train-size',
            '999',
            '--test-size',
            '500',
            '--step-size',
            '120',
            '--output',
            str(output),
            '--json',
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['auto_resized'] is True
    assert payload['runs'] >= 1
    assert output.exists()


def test_research_markets_filters_and_alpha_score(tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    _write_iso_history(history)
    runner = CliRunner()

    filtered = runner.invoke(
        cli,
        [
            'research',
            'markets',
            '--history-file',
            str(history),
            '--min-points',
            '10',
            '--min-volume',
            '500',
            '--min-span-seconds',
            '1',
            '--json',
        ],
    )
    assert filtered.exit_code == 0
    payload = json.loads(filtered.output)
    assert payload['count'] == 1
    assert payload['markets'][0]['market_id'] == 'M1'

    runs = tmp_path / 'runs.jsonl'
    runs.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'name': 'hi-dd',
                        'ok': True,
                        'metrics': {
                            'total_pnl': '50',
                            'max_drawdown': '0.20',
                            'total_trades': 50,
                        },
                    }
                ),
                json.dumps(
                    {
                        'name': 'balanced',
                        'ok': True,
                        'metrics': {
                            'total_pnl': '40',
                            'max_drawdown': '0.02',
                            'total_trades': 10,
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    ranked = runner.invoke(
        cli,
        [
            'research',
            'compare-runs',
            '--input-file',
            str(runs),
            '--sort-key',
            'alpha_score',
            '--top',
            '1',
            '--json',
        ],
    )
    assert ranked.exit_code == 0
    ranked_payload = json.loads(ranked.output)
    assert ranked_payload['top'][0]['name'] == 'balanced'


def test_research_alpha_pipeline(monkeypatch, tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    _write_iso_history(history)
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

    def fake_run_backtest_once(**kwargs):
        return {
            'total_trades': 8,
            'total_pnl': '12.0',
            'sharpe_ratio': '0.7',
            'win_rate': '0.55',
            'max_drawdown': '0.12',
        }

    monkeypatch.setattr(
        'coinjure.cli.research_commands._run_strategy_dry_run', fake_dry_run
    )
    monkeypatch.setattr(
        'coinjure.cli.research_commands._run_backtest_once', fake_run_backtest_once
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'research',
            'alpha-pipeline',
            '--history-file',
            str(history),
            '--strategy-ref',
            'dummy.module:Dummy',
            '--market-sort-by',
            'points',
            '--market-rank',
            '1',
            '--min-trades',
            '1',
            '--min-total-pnl',
            '0',
            '--max-drawdown-pct',
            '0.30',
            '--no-run-batch-markets',
            '--artifacts-dir',
            str(artifacts_dir),
            '--json',
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['passed'] is True
    assert payload['selected_market']['market_id'] == 'M1'
    assert (artifacts_dir / 'preflight.json').exists()
    assert (artifacts_dir / 'backtest_single.json').exists()
    assert (artifacts_dir / 'stress.jsonl').exists()
    assert (artifacts_dir / 'gate.json').exists()
