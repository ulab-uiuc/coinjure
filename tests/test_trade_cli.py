from __future__ import annotations

import json

from click.testing import CliRunner

from coinjure.cli.cli import cli


def test_engine_pause_resume_stop(monkeypatch):
    calls: list[str] = []

    def fake_run_command(cmd, socket_path=None, **kwargs):
        calls.append(cmd)
        return {'ok': True, 'status': 'paused' if cmd == 'pause' else 'stopping'}

    monkeypatch.setattr('coinjure.cli.engine_commands.run_command', fake_run_command)
    runner = CliRunner()

    pause = runner.invoke(cli, ['engine', 'pause'])
    resume = runner.invoke(cli, ['engine', 'resume'])
    stop = runner.invoke(cli, ['engine', 'stop'])

    assert pause.exit_code == 0
    assert resume.exit_code == 0
    assert stop.exit_code == 0
    assert calls == ['pause', 'resume', 'stop']


def test_engine_status_human_and_json(monkeypatch):
    def fake_run_command(cmd, socket_path=None, **kwargs):
        assert cmd == 'status'
        return {
            'ok': True,
            'paused': False,
            'runtime': '0:00:05',
            'event_count': 12,
            'decisions': 4,
            'executed': 2,
            'orders': 3,
        }

    monkeypatch.setattr('coinjure.cli.engine_commands.run_command', fake_run_command)
    runner = CliRunner()

    human = runner.invoke(cli, ['engine', 'status'])
    js = runner.invoke(cli, ['engine', 'status', '--json'])

    assert human.exit_code == 0
    assert 'events=12' in human.output
    assert js.exit_code == 0
    assert '"event_count": 12' in js.output


def test_engine_error_returns_nonzero(monkeypatch):
    def fake_run_command(cmd, socket_path=None, **kwargs):
        return {'ok': False, 'error': 'no socket'}

    monkeypatch.setattr('coinjure.cli.engine_commands.run_command', fake_run_command)
    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'pause'])
    assert result.exit_code == 1
    assert 'error: no socket' in result.output


def test_engine_killswitch_toggle(tmp_path):
    runner = CliRunner()
    kill_file = tmp_path / 'kill.switch'

    enable = runner.invoke(
        cli, ['engine', 'killswitch', '--on', '--path', str(kill_file)]
    )
    assert enable.exit_code == 0
    assert kill_file.exists()

    status = runner.invoke(
        cli, ['engine', 'killswitch', '--path', str(kill_file), '--json']
    )
    assert status.exit_code == 0
    assert '"status": "enabled"' in status.output

    disable = runner.invoke(
        cli, ['engine', 'killswitch', '--off', '--path', str(kill_file)]
    )
    assert disable.exit_code == 0
    assert not kill_file.exists()


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

    for spread_type in (
        'implication',
        'exclusivity',
        'correlated',
        'structural',
        'conditional',
        'temporal',
    ):
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
            relation_id='rel-1',
            spread_type='implication',
            status='backtest_passed',
        ),
        MarketRelation(
            relation_id='rel-2',
            spread_type='exclusivity',
            status='backtest_passed',
        ),
    ]

    monkeypatch.setattr('coinjure.cli.engine_commands.subprocess.Popen', fake_popen)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.REGISTRY_PATH', tmp_path / 'portfolio.json'
    )
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._load_relations_for_batch',
        lambda status: fake_relations,
    )
    # Pretend hub is already running
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.HUB_SOCKET_PATH',
        tmp_path / 'hub.sock',
    )
    (tmp_path / 'hub.sock').touch()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'paper-run',
            '--all-relations',
            '--json',
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(spawned) == 2


def test_paper_run_all_relations_mutually_exclusive_with_strategy_ref():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'paper-run',
            '--all-relations',
            '--strategy-ref',
            'foo:Bar',
        ],
    )
    assert result.exit_code != 0


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

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'paper-run',
            '--strategy-ref',
            'coinjure.strategy.demo:DemoStrategy',
            '--detach',
            '--json',
        ],
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().split('\n')[-1])
    assert out['ok'] is True
    assert out['pid'] == 12345
    assert len(spawned) == 1
    assert '--no-detach' in spawned[0]


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
        MarketRelation(
            relation_id='rel-d1',
            spread_type='implication',
            status='deployed',
        ),
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
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._confirm_live_trading',
        lambda as_json: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'live-run',
            '--all-relations',
            '--json',
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(spawned) == 1


def test_live_run_all_relations_mutually_exclusive_with_strategy_ref(monkeypatch):
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._confirm_live_trading',
        lambda as_json: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'live-run',
            '--all-relations',
            '--strategy-ref',
            'foo:Bar',
        ],
    )
    assert result.exit_code != 0


def test_live_run_detach(monkeypatch, tmp_path):
    """--detach spawns a subprocess and registers in the registry."""
    spawned = []

    class FakeProcess:
        pid = 54321

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProcess()

    monkeypatch.setattr('coinjure.cli.engine_commands.subprocess.Popen', fake_popen)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands.REGISTRY_PATH', tmp_path / 'portfolio.json'
    )
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._confirm_live_trading',
        lambda as_json: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'engine',
            'live-run',
            '--strategy-ref',
            'coinjure.strategy.demo:DemoStrategy',
            '--detach',
            '--json',
        ],
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output.strip().split('\n')[-1])
    assert out['ok'] is True
    assert out['pid'] == 54321
    assert len(spawned) == 1
    assert '--no-detach' in spawned[0]


def test_engine_promote_single(monkeypatch, tmp_path):
    """Promote a single relation from backtest_passed to deployed."""
    import json as json_mod

    from coinjure.engine.registry import StrategyEntry, StrategyRegistry
    from coinjure.market.relations import MarketRelation, RelationStore

    rel_path = tmp_path / 'relations.json'
    reg_path = tmp_path / 'portfolio.json'

    # Seed a backtest_passed relation
    store = RelationStore(path=rel_path)
    store.add(
        MarketRelation(
            relation_id='rel-p1',
            spread_type='implication',
            status='backtest_passed',
        )
    )

    # Seed a paper_trading registry entry
    reg = StrategyRegistry(path=reg_path)
    reg.add(
        StrategyEntry(
            strategy_id='rel-p1',
            strategy_ref='mod:Cls',
            relation_id='rel-p1',
            lifecycle='paper_trading',
        )
    )

    monkeypatch.setattr('coinjure.cli.engine_commands.REGISTRY_PATH', reg_path)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._get_relation_store_path', lambda: rel_path
    )

    from click.testing import CliRunner

    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'promote', 'rel-p1', '--json'])
    assert result.exit_code == 0, result.output

    out = json_mod.loads(result.output.strip())
    assert out['ok'] is True

    # Verify relation status updated
    updated = RelationStore(path=rel_path).get('rel-p1')
    assert updated.status == 'deployed'


def test_engine_promote_not_found(monkeypatch, tmp_path):
    rel_path = tmp_path / 'relations.json'
    reg_path = tmp_path / 'portfolio.json'

    monkeypatch.setattr('coinjure.cli.engine_commands.REGISTRY_PATH', reg_path)
    monkeypatch.setattr(
        'coinjure.cli.engine_commands._get_relation_store_path', lambda: rel_path
    )

    from click.testing import CliRunner

    from coinjure.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ['engine', 'promote', 'nonexistent'])
    assert result.exit_code != 0
