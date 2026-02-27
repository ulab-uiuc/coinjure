from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


def _write_history_iso(path: Path) -> None:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    points = []
    for i in range(16):
        points.append(
            {
                't': (base + timedelta(hours=i)).isoformat(),
                'p': round(0.40 + (i % 5) * 0.01, 4),
            }
        )
    rows = [{'event_id': 'E1', 'market_id': 'M1', 'time_series': {'Yes': points}}]
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


def test_research_slice_supports_iso_timestamps(tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {
                'Yes': [
                    {'t': '1970-01-01T00:00:01+00:00', 'p': 0.40},
                    {'t': '1970-01-01T00:00:02Z', 'p': 0.45},
                    {'t': '1970-01-01T00:00:03+00:00', 'p': 0.55},
                ]
            },
        }
    ]
    history.write_text(
        '\n'.join(json.dumps(row) for row in rows) + '\n',
        encoding='utf-8',
    )
    sliced = tmp_path / 'slice_iso.jsonl'

    runner = CliRunner()
    result = runner.invoke(
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
            '3',
            '--output',
            str(sliced),
            '--json',
        ],
    )
    assert result.exit_code == 0
    assert '"points": 2' in result.output


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


def test_research_walk_forward_supports_iso_timestamps(tmp_path: Path) -> None:
    history = tmp_path / 'history_iso.jsonl'
    _write_history_iso(history)
    output = tmp_path / 'wf.jsonl'

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
            '8',
            '--test-size',
            '4',
            '--step-size',
            '2',
            '--output',
            str(output),
            '--json',
        ],
    )
    assert result.exit_code == 0
    rows = _load_jsonl(output)
    assert len(rows) == 3
    assert all(row['ok'] for row in rows)


def test_research_slice_supports_json_array_history(tmp_path: Path) -> None:
    history = tmp_path / 'history.json'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 1, 'p': 0.40}, {'t': 2, 'p': 0.45}]},
        },
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 3, 'p': 0.50}, {'t': 4, 'p': 0.55}]},
        },
    ]
    history.write_text(json.dumps(rows), encoding='utf-8')
    sliced = tmp_path / 'slice.jsonl'

    runner = CliRunner()
    result = runner.invoke(
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
            '--output',
            str(sliced),
            '--json',
        ],
    )
    assert result.exit_code == 0
    out_rows = _load_jsonl(sliced)
    assert len(out_rows) == 4
