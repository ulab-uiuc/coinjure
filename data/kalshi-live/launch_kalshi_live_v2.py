"""Launch Kalshi live trading v2 — maker orders + preflight checks.

Focuses on exhaustive (complementary) markets with maker orders for 0 fees.
Uses preflight checks to prevent partial arb exposure.
"""
import json
import os
import subprocess
import sys
import time

os.chdir('/Users/ethanyang/prediction-market-cli')
from dotenv import load_dotenv
load_dotenv()

from coinjure.market.relations import RelationStore

store = RelationStore()

# Only target EXHAUSTIVE markets where sum(YES) must = 1.0
# These are the only markets where group arb is guaranteed profitable.
TARGETS = [
    {
        'relation_id': 'comp-KXFOMCDISSENTCOUNT-26MAR-1-KXFOMCDISSENTCOUNT-26MAR-2-KXFOMCDISSENTCOUNT-26MAR-3-+1',
        'events': ['KXFOMCDISSENTCOUNT-26MAR'],
        'note': 'FOMC March dissent count — resolves March 18-19',
    },
    {
        'relation_id': 'comp-KXFEDDECISION-26APR-C25-KXFEDDECISION-26APR-C26-KXFEDDECISION-26APR-H0-+1',
        'events': ['KXFEDDECISION-26APR'],
        'note': 'Fed April rate decision — resolves late April',
    },
]

STRATEGY_MAP = {
    'complementary': ('coinjure.strategy.builtin.group_arb_strategy', 'GroupArbStrategy'),
}

# Consolidated budget: fewer strategies, more capital each
TOTAL_BUDGET = 50.00  # User depositing $50
PER_BUDGET = round(TOTAL_BUDGET / len(TARGETS), 2)
DURATION = 4 * 24 * 3600  # 4 days (until FOMC meeting)

launched = []
for target in TARGETS:
    rid: str = str(target['relation_id'])
    events = target['events']
    rel = store.get(rid)
    if rel is None:
        print(f'SKIP {rid}: not found')
        continue

    strat_info = STRATEGY_MAP.get(rel.spread_type)
    if not strat_info:
        print(f'SKIP {rid}: no strategy for {rel.spread_type}')
        continue

    module_path, cls_name = strat_info
    kwargs = {
        'relation_id': rid,
        'trade_size': PER_BUDGET,
        'min_edge': 0.02,       # 2c minimum edge (lower threshold since maker has 0 fees)
        'use_maker_orders': True,
        'preflight_check': True,
        'cooldown_seconds': 300,  # 5 min cooldown between arbs
    }

    safe_id = rid.replace('/', '_').replace('+', 'p')[:40]

    script = f'''
import asyncio, os, sys, logging
os.chdir('/Users/ethanyang/prediction-market-cli')
from dotenv import load_dotenv; load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
from decimal import Decimal
from coinjure.data.live.kalshi import LiveKalshiDataSource
from coinjure.engine.runner import run_live_kalshi_trading
from {module_path} import {cls_name}

strategy = {cls_name}(**{repr(kwargs)})
data_source = LiveKalshiDataSource(
    event_cache_file='data/kalshi-live/cache_{safe_id}.jsonl',
    polling_interval=120.0,
    reprocess_on_start=False,
    watch_events={repr(events)},
)
asyncio.run(run_live_kalshi_trading(
    data_source=data_source,
    strategy=strategy,
    duration={DURATION}.0,
    max_position_size=Decimal('{PER_BUDGET}'),
    max_total_exposure=Decimal('{round(PER_BUDGET * 2, 2)}'),
    continuous=True,
    monitor=False,
    exchange_name='Kalshi',
))
'''

    script_path = f'/tmp/kalshi_live_v2_{safe_id}.py'
    with open(script_path, 'w') as f:
        f.write(script)

    log_path = f'data/kalshi-live/v2_{safe_id}.log'
    proc = subprocess.Popen(
        [sys.executable, script_path],
        stdout=open(log_path, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    launched.append({
        'relation_id': rid,
        'pid': proc.pid,
        'type': rel.spread_type,
        'markets': len(rel.markets),
        'strategy': cls_name,
        'budget': PER_BUDGET,
        'events': events,
        'note': target.get('note', ''),
        'log': log_path,
    })
    print(f'  {rid}')
    print(f'    pid={proc.pid}  ${PER_BUDGET}  {cls_name}  maker=True  preflight=True')
    print(f'    events={events}  note={target.get("note", "")}')
    time.sleep(1)

print(f'\nLaunched {len(launched)} Kalshi live v2 instances')
print(f'Budget: ${TOTAL_BUDGET} total, ${PER_BUDGET}/ea')
print(f'Duration: {DURATION // 3600} hours')
print(f'Mode: maker orders (0 fees) + preflight checks')

with open('data/kalshi-live/manifest_v2.json', 'w') as f:
    json.dump({
        'launched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total_budget': TOTAL_BUDGET,
        'per_budget': PER_BUDGET,
        'duration_hours': DURATION // 3600,
        'mode': 'maker_orders_with_preflight',
        'instances': launched,
    }, f, indent=2)
