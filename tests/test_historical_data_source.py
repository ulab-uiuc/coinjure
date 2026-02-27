from __future__ import annotations

import json
from pathlib import Path

from coinjure.data.backtest.historical_data_source import HistoricalDataSource
from coinjure.ticker.ticker import PolyMarketTicker


def _ticker() -> PolyMarketTicker:
    return PolyMarketTicker(
        symbol='YES',
        name='Test Market',
        token_id='YES',
        no_token_id='NO',
        market_id='M1',
        event_id='E1',
    )


def test_historical_data_source_supports_json_array(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.json'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {'Yes': [{'t': 2, 'p': 0.45}, {'t': 1, 'p': 0.40}]},
        },
        {
            'event_id': 'E2',
            'market_id': 'M2',
            'time_series': {'Yes': [{'t': 1, 'p': 0.90}]},
        },
    ]
    history_file.write_text(json.dumps(rows), encoding='utf-8')

    source = HistoricalDataSource(str(history_file), _ticker())
    assert len(source.events) == 4
    assert source.events[0].timestamp == 1
    assert source.events[-1].timestamp == 2


def test_historical_data_source_sorts_iso_timestamps(tmp_path: Path) -> None:
    history_file = tmp_path / 'history.jsonl'
    rows = [
        {
            'event_id': 'E1',
            'market_id': 'M1',
            'time_series': {
                'Yes': [
                    {'t': '2025-01-02T00:00:00+00:00', 'p': 0.60},
                    {'t': '2025-01-01T00:00:00+00:00', 'p': 0.55},
                ]
            },
        }
    ]
    history_file.write_text(
        '\n'.join(json.dumps(row) for row in rows) + '\n',
        encoding='utf-8',
    )

    source = HistoricalDataSource(str(history_file), _ticker())
    assert len(source.events) == 4
    assert source.events[0].timestamp == '2025-01-01T00:00:00+00:00'
    assert source.events[-1].timestamp == '2025-01-02T00:00:00+00:00'
