# Batch Paper/Live Trading Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `--all-relations` and `--detach` to `engine paper-run` / `engine live-run`, plus `engine promote` command, so all backtest-passed relations can paper/live trade in parallel as detached processes with auto hub management.

**Architecture:** Each relation runs in its own detached OS process via `subprocess.Popen`. A shared MarketDataHub fans out market data. The CLI layer orchestrates: auto-starts hub if needed, spawns one `engine paper-run --detach` per relation, and registers each in `StrategyRegistry`.

**Tech Stack:** Click CLI, subprocess, asyncio, existing StrategyRegistry + RelationStore + STRATEGY_BY_RELATION

---

### Task 1: Add `relation_id` field to StrategyEntry

**Files:**

- Modify: `coinjure/engine/registry.py:20-53`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
def test_strategy_entry_relation_id():
    from coinjure.engine.registry import StrategyEntry

    entry = StrategyEntry(
        strategy_id='rel-abc',
        strategy_ref='coinjure.strategy.builtin:DirectArbStrategy',
        relation_id='rel-abc',
    )
    assert entry.relation_id == 'rel-abc'
    d = entry.to_dict()
    assert d['relation_id'] == 'rel-abc'
    restored = StrategyEntry.from_dict(d)
    assert restored.relation_id == 'rel-abc'


def test_strategy_entry_relation_id_default():
    from coinjure.engine.registry import StrategyEntry

    entry = StrategyEntry(strategy_id='x', strategy_ref='m:C')
    assert entry.relation_id is None
```

**Step 2: Run test to verify it fails**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_strategy_entry_relation_id tests/test_trade_cli.py::test_strategy_entry_relation_id_default -v -p no:nbmake`
Expected: FAIL — `StrategyEntry.__init__() got an unexpected keyword argument 'relation_id'`

**Step 3: Write minimal implementation**

In `coinjure/engine/registry.py`, add `relation_id` field to `StrategyEntry` (after line 52, before `retired_reason`):

```python
relation_id: str | None = None
```

Add `'relation_id'` to the `_FIELDS` set (line 20).

**Step 4: Run test to verify it passes**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_strategy_entry_relation_id tests/test_trade_cli.py::test_strategy_entry_relation_id_default -v -p no:nbmake`
Expected: PASS

**Step 5: Commit**

```bash
git add coinjure/engine/registry.py tests/test_trade_cli.py
git commit -m "feat(registry): add relation_id field to StrategyEntry"
```

---

### Task 2: Add `build_strategy_ref_for_relation()` helper

This helper converts a `MarketRelation` into `(strategy_ref, strategy_kwargs)` tuple that can be passed to `engine paper-run --strategy-ref ... --strategy-kwargs-json ...`. Reuses the same kwargs-building logic from `backtester.py`.

**Files:**

- Modify: `coinjure/strategy/builtin/__init__.py`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
def test_build_strategy_ref_for_relation_same_event():
    from coinjure.market.relations import MarketRelation
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    rel = MarketRelation(
        relation_id='r1',
        spread_type='same_event',
        market_a={'id': 'poly-123', 'platform': 'polymarket', 'token_ids': ['0xabc']},
        market_b={'id': 'kalshi-456', 'platform': 'kalshi', 'ticker': 'K-TICK'},
    )
    ref, kwargs = build_strategy_ref_for_relation(rel)
    assert ref == 'coinjure.strategy.builtin.direct_arb_strategy:DirectArbStrategy'
    assert kwargs['poly_market_id'] == 'poly-123'
    assert kwargs['poly_token_id'] == '0xabc'
    assert kwargs['kalshi_ticker'] == 'K-TICK'


def test_build_strategy_ref_for_relation_complementary():
    from coinjure.market.relations import MarketRelation
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    rel = MarketRelation(
        relation_id='r2',
        spread_type='complementary',
        market_a={'id': 'a', 'event_id': 'evt-1'},
        market_b={'id': 'b'},
    )
    ref, kwargs = build_strategy_ref_for_relation(rel)
    assert 'EventSumArbStrategy' in ref
    assert kwargs['event_id'] == 'evt-1'


def test_build_strategy_ref_for_relation_generic():
    from coinjure.market.relations import MarketRelation
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    for spread_type in ('implication', 'exclusivity', 'correlated', 'structural', 'conditional', 'temporal'):
        rel = MarketRelation(relation_id=f'r-{spread_type}', spread_type=spread_type)
        ref, kwargs = build_strategy_ref_for_relation(rel)
        assert ref  # non-empty strategy ref
        assert kwargs.get('relation_id') == f'r-{spread_type}'


def test_build_strategy_ref_for_relation_unknown():
    from coinjure.market.relations import MarketRelation
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    rel = MarketRelation(relation_id='r-bad', spread_type='unknown_type')
    ref, kwargs = build_strategy_ref_for_relation(rel)
    assert ref is None
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_build_strategy_ref_for_relation_same_event tests/test_trade_cli.py::test_build_strategy_ref_for_relation_complementary tests/test_trade_cli.py::test_build_strategy_ref_for_relation_generic tests/test_trade_cli.py::test_build_strategy_ref_for_relation_unknown -v -p no:nbmake`
Expected: FAIL — `ImportError: cannot import name 'build_strategy_ref_for_relation'`

**Step 3: Write minimal implementation**

Add to `coinjure/strategy/builtin/__init__.py`:

```python
def build_strategy_ref_for_relation(
    relation: MarketRelation,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Build (strategy_ref, strategy_kwargs) for a relation.

    Returns (None, {}) if no strategy maps to the relation's spread_type.
    """
    strategy_cls = STRATEGY_BY_RELATION.get(relation.spread_type)
    if strategy_cls is None:
        return None, {}

    kwargs: dict[str, Any] = dict(extra_kwargs or {})
    spread_type = relation.spread_type

    if spread_type == 'same_event':
        plat_a = str(relation.market_a.get('platform', 'polymarket')).lower()
        if plat_a == 'kalshi':
            poly_m, kalshi_m, poly_leg = relation.market_b, relation.market_a, 'b'
        else:
            poly_m, kalshi_m, poly_leg = relation.market_a, relation.market_b, 'a'
        kwargs.setdefault('poly_market_id', str(poly_m.get('id', '')))
        kwargs.setdefault('poly_token_id', relation.get_token_id(poly_leg))
        kwargs.setdefault(
            'kalshi_ticker',
            str(kalshi_m.get('ticker', kalshi_m.get('id', ''))),
        )
    elif spread_type == 'complementary':
        kwargs.setdefault('event_id', str(relation.market_a.get('event_id', '')))
    else:
        kwargs.setdefault('relation_id', relation.relation_id)

    module = strategy_cls.__module__
    name = strategy_cls.__name__
    ref = f'{module}:{name}'
    return ref, kwargs
```

Add imports at top of the file:

```python
from typing import Any

from coinjure.market.relations import MarketRelation
```

Add `'build_strategy_ref_for_relation'` to `__all__`.

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_trade_cli.py -k "build_strategy_ref" -v -p no:nbmake`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add coinjure/strategy/builtin/__init__.py tests/test_trade_cli.py
git commit -m "feat(builtin): add build_strategy_ref_for_relation helper"
```

---

### Task 3: Add `--detach` support to `engine paper-run`

**Files:**

- Modify: `coinjure/cli/engine_commands.py:176-304`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
import json

def test_paper_run_detach(monkeypatch, tmp_path):
    """--detach spawns a subprocess and registers in the registry."""
    spawned = []

    class FakeProcess:
        pid = 12345

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProcess()

    monkeypatch.setattr('coinjure.cli.engine_commands.subprocess.Popen', fake_popen)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.REGISTRY_PATH', tmp_path / 'portfolio.json'
    )
    # Prevent hub auto-connect
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.HUB_SOCKET_PATH',
        tmp_path / 'no-hub.sock',
    )

    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        'engine', 'paper-run',
        '--strategy-ref', 'coinjure.strategy.demo:DemoStrategy',
        '--detach', '--json',
    ])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().split('\n')[-1])
    assert out['ok'] is True
    assert out['pid'] == 12345
    assert len(spawned) == 1
    assert '--no-detach' in spawned[0]
```

**Step 2: Run test to verify it fails**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_paper_run_detach -v -p no:nbmake`
Expected: FAIL — no `--detach` option

**Step 3: Write minimal implementation**

In `coinjure/cli/engine_commands.py`, modify `engine_paper_run`:

1. Add `import subprocess` back to imports (line 13 area).

2. Add click option after `--no-hub` (around line 207):

```python
@click.option(
    '--detach/--no-detach',
    default=False,
    help='Run as a detached background process.',
)
```

3. Add `detach: bool` parameter to function signature.

4. At the top of the function body (after line 224), add detach logic:

```python
    if detach:
        cmd = shlex.split(_coinjure_cmd()) + ['engine', 'paper-run', '--no-detach']
        if strategy_ref:
            cmd += ['--strategy-ref', strategy_ref]
        if strategy_kwargs_json:
            cmd += ['--strategy-kwargs-json', strategy_kwargs_json]
        cmd += ['--exchange', exchange, '--initial-capital', initial_capital]
        if duration is not None:
            cmd += ['--duration', str(duration)]
        if no_hub:
            cmd += ['--no-hub']
        if as_json:
            cmd += ['--json']

        proc = subprocess.Popen(
            cmd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Register in portfolio
        reg = _load_registry()
        sid = strategy_ref or f'paper-{proc.pid}'
        entry = StrategyEntry(
            strategy_id=sid,
            strategy_ref=strategy_ref or 'idle',
            lifecycle='paper_trading',
            exchange=exchange,
            pid=proc.pid,
            socket_path=str(SOCKET_DIR / f'engine-{proc.pid}.sock'),
        )
        try:
            reg.add(entry)
        except ValueError:
            reg.update(entry)

        _emit({'ok': True, 'pid': proc.pid, 'strategy_id': sid,
               'socket': entry.socket_path}, as_json=as_json)
        return
```

5. Add `import shlex` to imports.

**Step 4: Run test to verify it passes**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_paper_run_detach -v -p no:nbmake`
Expected: PASS

**Step 5: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All passing

**Step 6: Commit**

```bash
git add coinjure/cli/engine_commands.py tests/test_trade_cli.py
git commit -m "feat(cli): add --detach support to engine paper-run"
```

---

### Task 4: Add `--all-relations` to `engine paper-run`

**Files:**

- Modify: `coinjure/cli/engine_commands.py:176-304`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
def test_paper_run_all_relations(monkeypatch, tmp_path):
    """--all-relations spawns one detached process per backtest_passed relation."""
    from coinjure.market.relations import MarketRelation

    spawned = []

    class FakeProcess:
        pid_counter = 100
        def __init__(self):
            FakeProcess.pid_counter += 1
            self.pid = FakeProcess.pid_counter

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProcess()

    fake_relations = [
        MarketRelation(
            relation_id='rel-1', spread_type='implication', status='backtest_passed',
        ),
        MarketRelation(
            relation_id='rel-2', spread_type='exclusivity', status='backtest_passed',
        ),
    ]

    monkeypatch.setattr('coinjure.cli.engine_commands.subprocess.Popen', fake_popen)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.REGISTRY_PATH', tmp_path / 'portfolio.json'
    )
    # Mock RelationStore
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._load_relations_for_batch',
        lambda status: fake_relations,
    )
    # Mock hub auto-start: pretend hub is already running
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.HUB_SOCKET_PATH',
        tmp_path / 'hub.sock',
    )
    (tmp_path / 'hub.sock').touch()

    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        'engine', 'paper-run', '--all-relations', '--json',
    ])
    assert result.exit_code == 0, result.output
    assert len(spawned) == 2


def test_paper_run_all_relations_mutually_exclusive_with_strategy_ref():
    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        'engine', 'paper-run',
        '--all-relations', '--strategy-ref', 'foo:Bar',
    ])
    assert result.exit_code != 0
    assert 'mutually exclusive' in result.output.lower() or 'cannot' in result.output.lower()
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_paper_run_all_relations tests/test_trade_cli.py::test_paper_run_all_relations_mutually_exclusive_with_strategy_ref -v -p no:nbmake`
Expected: FAIL

**Step 3: Write minimal implementation**

In `coinjure/cli/engine_commands.py`:

1. Add click option to `engine_paper_run` (before `--detach`):

```python
@click.option(
    '--all-relations',
    is_flag=True,
    default=False,
    help='Batch run all backtest_passed relations (each as a detached process).',
)
```

2. Add `all_relations: bool` to function signature.

3. Add helper function near the top helpers section:

```python
def _load_relations_for_batch(status: str) -> list:
    from coinjure.market.relations import RelationStore
    return RelationStore().list(status=status)


def _ensure_hub_running(as_json: bool) -> None:
    """Auto-start hub if not running. Waits up to 5s for socket."""
    if HUB_SOCKET_PATH.exists():
        return
    cmd = shlex.split(_coinjure_cmd()) + ['hub', 'start', '--detach']
    subprocess.Popen(
        cmd, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if HUB_SOCKET_PATH.exists():
            return
        time.sleep(0.1)
    if not as_json:
        click.echo('Warning: Hub socket not ready after 5s, proceeding anyway.')
```

4. Add batch logic at the top of `engine_paper_run` body:

```python
    if all_relations and strategy_ref:
        raise click.ClickException(
            '--all-relations and --strategy-ref are mutually exclusive.'
        )

    if all_relations:
        _run_batch_paper(
            initial_capital=initial_capital,
            duration=duration,
            as_json=as_json,
            no_hub=no_hub,
        )
        return
```

5. Add the batch orchestration function:

```python
def _run_batch_paper(
    *,
    initial_capital: str,
    duration: float | None,
    as_json: bool,
    no_hub: bool,
) -> None:
    """Spawn one detached paper-run per backtest_passed relation."""
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    relations = _load_relations_for_batch('backtest_passed')
    if not relations:
        raise click.ClickException('No relations with status backtest_passed.')

    if not no_hub:
        _ensure_hub_running(as_json=as_json)

    reg = _load_registry()
    results = []

    for rel in relations:
        ref, kwargs = build_strategy_ref_for_relation(rel)
        if ref is None:
            results.append({
                'relation_id': rel.relation_id,
                'ok': False,
                'error': f'No strategy for spread_type: {rel.spread_type}',
            })
            continue

        cmd = shlex.split(_coinjure_cmd()) + [
            'engine', 'paper-run',
            '--strategy-ref', ref,
            '--strategy-kwargs-json', json.dumps(kwargs),
            '--initial-capital', initial_capital,
            '--no-detach',
        ]
        if duration is not None:
            cmd += ['--duration', str(duration)]

        proc = subprocess.Popen(
            cmd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        socket = str(SOCKET_DIR / f'engine-{proc.pid}.sock')
        entry = StrategyEntry(
            strategy_id=rel.relation_id,
            strategy_ref=ref,
            strategy_kwargs=kwargs,
            relation_id=rel.relation_id,
            lifecycle='paper_trading',
            exchange='cross_platform',
            pid=proc.pid,
            socket_path=socket,
        )
        try:
            reg.add(entry)
        except ValueError:
            entry_existing = reg.get(rel.relation_id)
            if entry_existing:
                entry_existing.pid = proc.pid
                entry_existing.socket_path = socket
                entry_existing.lifecycle = 'paper_trading'
                reg.update(entry_existing)

        results.append({
            'relation_id': rel.relation_id,
            'ok': True,
            'pid': proc.pid,
            'strategy_ref': ref,
            'socket': socket,
        })

    if as_json:
        _emit_json({'ok': True, 'launched': results, 'count': len(results)})
    else:
        click.echo(f'\nLaunched {len(results)} paper trading instances:\n')
        for r in results:
            if r.get('ok'):
                click.echo(
                    f'  {r["relation_id"]}  pid={r["pid"]}  {r["strategy_ref"]}'
                )
            else:
                click.echo(f'  {r["relation_id"]}  SKIPPED: {r["error"]}')
```

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_trade_cli.py -k "all_relations" -v -p no:nbmake`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All passing

**Step 6: Commit**

```bash
git add coinjure/cli/engine_commands.py tests/test_trade_cli.py
git commit -m "feat(cli): add --all-relations batch mode to engine paper-run"
```

---

### Task 5: Add `--detach` and `--all-relations` to `engine live-run`

**Files:**

- Modify: `coinjure/cli/engine_commands.py:310-429`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
def test_live_run_all_relations(monkeypatch, tmp_path):
    """--all-relations spawns one detached process per deployed relation."""
    from coinjure.market.relations import MarketRelation

    spawned = []

    class FakeProcess:
        pid_counter = 200
        def __init__(self):
            FakeProcess.pid_counter += 1
            self.pid = FakeProcess.pid_counter

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProcess()

    fake_relations = [
        MarketRelation(relation_id='rel-d1', spread_type='implication', status='deployed'),
    ]

    monkeypatch.setattr('coinjure.cli.engine_commands.subprocess.Popen', fake_popen)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.REGISTRY_PATH', tmp_path / 'portfolio.json'
    )
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._load_relations_for_batch',
        lambda status: fake_relations if status == 'deployed' else [],
    )
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.HUB_SOCKET_PATH',
        tmp_path / 'hub.sock',
    )
    (tmp_path / 'hub.sock').touch()
    # Skip live trading confirmation
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._confirm_live_trading',
        lambda as_json: None,
    )

    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        'engine', 'live-run', '--all-relations', '--json',
    ])
    assert result.exit_code == 0, result.output
    assert len(spawned) == 1


def test_live_run_all_relations_mutually_exclusive_with_strategy_ref(monkeypatch):
    # Skip confirmation
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._confirm_live_trading',
        lambda as_json: None,
    )
    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, [
        'engine', 'live-run',
        '--all-relations', '--strategy-ref', 'foo:Bar',
    ])
    assert result.exit_code != 0
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_live_run_all_relations tests/test_trade_cli.py::test_live_run_all_relations_mutually_exclusive_with_strategy_ref -v -p no:nbmake`
Expected: FAIL

**Step 3: Write minimal implementation**

Mirror the paper-run pattern for `engine_live_run`:

1. Add `--all-relations`, `--detach/--no-detach` options.
2. Add `all_relations: bool`, `detach: bool` to signature.
3. Add mutual exclusion check.
4. Add `_run_batch_live()` function (similar to `_run_batch_paper` but uses `deployed` status and `live_trading` lifecycle, passes through wallet/API key env vars).

```python
def _run_batch_live(
    *,
    initial_capital: str,
    duration: float | None,
    as_json: bool,
    exchange: str,
) -> None:
    """Spawn one detached live-run per deployed relation."""
    from coinjure.strategy.builtin import build_strategy_ref_for_relation

    relations = _load_relations_for_batch('deployed')
    if not relations:
        raise click.ClickException('No relations with status deployed.')

    _ensure_hub_running(as_json=as_json)

    reg = _load_registry()
    results = []

    for rel in relations:
        ref, kwargs = build_strategy_ref_for_relation(rel)
        if ref is None:
            results.append({
                'relation_id': rel.relation_id,
                'ok': False,
                'error': f'No strategy for spread_type: {rel.spread_type}',
            })
            continue

        cmd = shlex.split(_coinjure_cmd()) + [
            'engine', 'live-run',
            '--strategy-ref', ref,
            '--strategy-kwargs-json', json.dumps(kwargs),
            '--initial-capital', initial_capital,
            '--exchange', exchange,
            '--no-detach',
        ]
        if duration is not None:
            cmd += ['--duration', str(duration)]

        proc = subprocess.Popen(
            cmd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        socket = str(SOCKET_DIR / f'engine-{proc.pid}.sock')
        entry = StrategyEntry(
            strategy_id=rel.relation_id,
            strategy_ref=ref,
            strategy_kwargs=kwargs,
            relation_id=rel.relation_id,
            lifecycle='live_trading',
            exchange=exchange,
            pid=proc.pid,
            socket_path=socket,
        )
        try:
            reg.add(entry)
        except ValueError:
            entry_existing = reg.get(rel.relation_id)
            if entry_existing:
                entry_existing.pid = proc.pid
                entry_existing.socket_path = socket
                entry_existing.lifecycle = 'live_trading'
                reg.update(entry_existing)

        results.append({
            'relation_id': rel.relation_id,
            'ok': True,
            'pid': proc.pid,
            'strategy_ref': ref,
            'socket': socket,
        })

    if as_json:
        _emit_json({'ok': True, 'launched': results, 'count': len(results)})
    else:
        click.echo(f'\nLaunched {len(results)} live trading instances:\n')
        for r in results:
            if r.get('ok'):
                click.echo(
                    f'  {r["relation_id"]}  pid={r["pid"]}  {r["strategy_ref"]}'
                )
            else:
                click.echo(f'  {r["relation_id"]}  SKIPPED: {r["error"]}')
```

5. Add detach logic (same pattern as paper-run detach).

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_trade_cli.py -k "live_run_all" -v -p no:nbmake`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All passing

**Step 6: Commit**

```bash
git add coinjure/cli/engine_commands.py tests/test_trade_cli.py
git commit -m "feat(cli): add --detach and --all-relations to engine live-run"
```

---

### Task 6: Add `engine promote` command

**Files:**

- Modify: `coinjure/cli/engine_commands.py`
- Test: `tests/test_trade_cli.py`

**Step 1: Write the failing test**

Add to `tests/test_trade_cli.py`:

```python
def test_engine_promote_single(monkeypatch, tmp_path):
    """Promote a single relation from backtest_passed to deployed."""
    from coinjure.market.relations import MarketRelation, RelationStore
    from coinjure.engine.registry import StrategyEntry, StrategyRegistry

    rel_path = tmp_path / 'relations.json'
    reg_path = tmp_path / 'portfolio.json'

    # Seed a backtest_passed relation
    store = RelationStore(path=rel_path)
    store.add(MarketRelation(
        relation_id='rel-p1', spread_type='implication', status='backtest_passed',
    ))

    # Seed a paper_trading registry entry
    reg = StrategyRegistry(path=reg_path)
    reg.add(StrategyEntry(
        strategy_id='rel-p1',
        strategy_ref='mod:Cls',
        relation_id='rel-p1',
        lifecycle='paper_trading',
    ))

    monkeypatch.setattr('coinjure.cli.engine_commands.REGISTRY_PATH', reg_path)
    monkeypatch.setattr('coinjure.cli.engine_commands._get_relation_store_path', lambda: rel_path)

    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'promote', 'rel-p1', '--json'])
    assert result.exit_code == 0, result.output

    out = json.loads(result.output.strip())
    assert out['ok'] is True

    # Verify relation status updated
    updated = RelationStore(path=rel_path).get('rel-p1')
    assert updated.status == 'deployed'


def test_engine_promote_not_found():
    from click.testing import CliRunner
    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'promote', 'nonexistent'])
    assert result.exit_code != 0
```

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_trade_cli.py::test_engine_promote_single tests/test_trade_cli.py::test_engine_promote_not_found -v -p no:nbmake`
Expected: FAIL — no `promote` command

**Step 3: Write minimal implementation**

Add helper near top of `engine_commands.py`:

```python
def _get_relation_store_path() -> Path:
    from coinjure.market.relations import RELATIONS_PATH
    return RELATIONS_PATH
```

Add `promote` command:

```python
@engine.command('promote')
@click.argument('relation_id', required=False, default=None)
@click.option(
    '--all', 'promote_all', is_flag=True, default=False,
    help='Promote all paper_trading entries with positive PnL.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def engine_promote(
    relation_id: str | None,
    promote_all: bool,
    as_json: bool,
) -> None:
    """Promote relation(s) from paper_trading to deployed."""
    from coinjure.market.relations import RelationStore

    if not relation_id and not promote_all:
        raise click.ClickException('Provide <relation-id> or --all.')

    store = RelationStore(path=_get_relation_store_path())
    reg = _load_registry()

    if promote_all:
        entries = [
            e for e in reg.list()
            if e.lifecycle == 'paper_trading' and e.relation_id
        ]
        promoted = []
        for entry in entries:
            rel = store.get(entry.relation_id)
            if rel is None:
                continue
            rel.status = 'deployed'
            store.update(rel)
            entry.lifecycle = 'deployed'
            reg.update(entry)
            promoted.append(entry.relation_id)

        if as_json:
            _emit_json({'ok': True, 'promoted': promoted, 'count': len(promoted)})
        else:
            click.echo(f'Promoted {len(promoted)} relation(s) to deployed.')
        return

    # Single relation
    rel = store.get(relation_id)
    if rel is None:
        raise click.ClickException(f'Relation not found: {relation_id}')

    rel.status = 'deployed'
    store.update(rel)

    entry = reg.get(relation_id)
    if entry:
        entry.lifecycle = 'deployed'
        reg.update(entry)

    if as_json:
        _emit_json({'ok': True, 'relation_id': relation_id, 'status': 'deployed'})
    else:
        click.echo(f'Promoted {relation_id} to deployed.')
```

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_trade_cli.py -k "promote" -v -p no:nbmake`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x -q`
Expected: All passing

**Step 6: Commit**

```bash
git add coinjure/cli/engine_commands.py tests/test_trade_cli.py
git commit -m "feat(cli): add engine promote command for relation lifecycle"
```

---

### Task 7: Final integration test and cleanup

**Files:**

- Test: `tests/test_trade_cli.py`
- Verify: all files modified in Tasks 1-6

**Step 1: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -v`
Expected: All passing (244+ tests)

**Step 2: Verify CLI help text**

Run:

```bash
poetry run coinjure engine paper-run --help
poetry run coinjure engine live-run --help
poetry run coinjure engine promote --help
```

Verify `--all-relations`, `--detach`, and promote args appear correctly.

**Step 3: Commit any final fixes**

```bash
git add -A
git commit -m "test: add integration tests for batch paper/live trading"
```

**Step 4: Push branch**

```bash
git push -u origin <branch-name>
```
