"""Top-level memory CLI group — persistent experiment ledger.

Commands
--------
  memory add      — append an experiment result to the ledger
  memory list     — list experiments with optional filters
  memory best     — return top-N experiments by a metric
  memory summary  — aggregate statistics across all experiments
"""

from __future__ import annotations

from datetime import datetime, timezone

import click

from coinjure.cli.research_helpers import _parse_json_object
from coinjure.cli.utils import _emit


@click.group()
def memory() -> None:
    """Persistent experiment memory (ledger)."""


@memory.command('add')
@click.option('--run-id', required=True, help='Unique experiment identifier.')
@click.option('--strategy-ref', required=True)
@click.option('--strategy-kwargs-json', default='{}', show_default=True)
@click.option('--market-id', default='')
@click.option('--event-id', default='')
@click.option('--history-file', default='')
@click.option('--gate-passed', is_flag=True, default=False)
@click.option('--metrics-json', default='{}', help='JSON object of metric values.')
@click.option('--tag', multiple=True, help='Tags (can repeat).')
@click.option('--notes', default='')
@click.option('--artifacts-dir', default='')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_add(
    run_id: str,
    strategy_ref: str,
    strategy_kwargs_json: str,
    market_id: str,
    event_id: str,
    history_file: str,
    gate_passed: bool,
    metrics_json: str,
    tag: tuple[str, ...],
    notes: str,
    artifacts_dir: str,
    as_json: bool,
) -> None:
    """Append an experiment result to the ledger."""
    from coinjure.research.ledger import ExperimentLedger, LedgerEntry

    strategy_kwargs = _parse_json_object(
        strategy_kwargs_json, option_name='--strategy-kwargs-json'
    )
    metrics = _parse_json_object(metrics_json, option_name='--metrics-json')
    entry = LedgerEntry(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        market_id=market_id,
        event_id=event_id,
        history_file=history_file,
        gate_passed=gate_passed,
        metrics=metrics,
        tags=list(tag),
        notes=notes,
        artifacts_dir=artifacts_dir,
    )
    ExperimentLedger().append(entry)
    _emit({'ok': True, 'run_id': run_id, 'entry': entry.to_dict()}, as_json=as_json)


@memory.command('list')
@click.option('--tag', default=None, help='Filter by tag.')
@click.option(
    '--strategy-ref', default=None, help='Filter by strategy ref (substring).'
)
@click.option('--market-id', default=None, help='Filter by exact market ID.')
@click.option('--gate-passed', is_flag=True, default=False, help='Only gate-passed.')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_list(
    tag: str | None,
    strategy_ref: str | None,
    market_id: str | None,
    gate_passed: bool,
    as_json: bool,
) -> None:
    """List experiments from the ledger with optional filters."""
    from coinjure.research.ledger import ExperimentLedger

    entries = ExperimentLedger().query(
        tag=tag,
        strategy_ref=strategy_ref,
        market_id=market_id,
        gate_passed=gate_passed if gate_passed else None,
    )
    _emit(
        {'ok': True, 'count': len(entries), 'entries': [e.to_dict() for e in entries]},
        as_json=as_json,
    )


@memory.command('best')
@click.option(
    '--metric', default='total_pnl', show_default=True, help='Metric key to rank by.'
)
@click.option('--top', default=5, show_default=True, type=int)
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_best(metric: str, top: int, as_json: bool) -> None:
    """Return top-N experiments by a metric."""
    from coinjure.research.ledger import ExperimentLedger

    entries = ExperimentLedger().best(metric_key=metric, top_n=top)
    _emit(
        {
            'ok': True,
            'metric': metric,
            'count': len(entries),
            'entries': [e.to_dict() for e in entries],
        },
        as_json=as_json,
    )


@memory.command('summary')
@click.option('--json', 'as_json', is_flag=True, default=False)
def memory_summary(as_json: bool) -> None:
    """Aggregate statistics across all experiments."""
    from coinjure.research.ledger import ExperimentLedger

    summary = ExperimentLedger().summary()
    _emit({'ok': True, **summary}, as_json=as_json)
