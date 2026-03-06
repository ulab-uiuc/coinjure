"""Shared arbitrage helper functions used by market and portfolio commands."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click

from coinjure.engine.registry import REGISTRY_PATH, StrategyEntry, StrategyRegistry
from coinjure.market.matching import MarketPair, match_markets

_DIRECT_ARB_REF = 'examples/strategies/direct_arb_strategy.py:DirectArbStrategy'
_EVENT_SUM_ARB_REF = 'examples/strategies/event_sum_arb_strategy.py:EventSumArbStrategy'

# Fee estimate: ~0.5% per side round-trip (conservative)
_FEE_ESTIMATE = Decimal('0.005')


# ── price helpers ─────────────────────────────────────────────────────────────


def _kalshi_price(yes_cents: Any) -> Decimal | None:
    """Convert Kalshi yes_ask/yes_bid (cents 0-100) to decimal 0-1."""
    try:
        v = Decimal(str(yes_cents))
        if v <= 0:
            return None
        return v / Decimal('100')
    except (InvalidOperation, TypeError):
        return None


def _poly_price(price_str: Any) -> Decimal | None:
    """Parse Polymarket best_bid/best_ask string (already 0-1)."""
    try:
        v = Decimal(str(price_str))
        if v <= 0:
            return None
        return v
    except (InvalidOperation, TypeError):
        return None


# ── portfolio helpers ─────────────────────────────────────────────────────────


def _portfolio_ids() -> set[str]:
    """Return all strategy_ids currently in the portfolio."""
    try:
        reg = StrategyRegistry()
        return {e.strategy_id for e in reg.list()}
    except Exception:
        return set()


def _pair_ids_in_portfolio(pairs: list[MarketPair]) -> set[str]:
    """Return (poly_id, kalshi_ticker) pair keys that are already in the portfolio."""
    try:
        reg = StrategyRegistry()
        entries = reg.list()
        in_portfolio: set[str] = set()
        for entry in entries:
            kwargs = entry.strategy_kwargs or {}
            poly_id = kwargs.get('poly_market_id', '')
            kalshi_ticker = kwargs.get('kalshi_ticker', '')
            if poly_id and kalshi_ticker:
                in_portfolio.add(f'{poly_id}::{kalshi_ticker}')
        return in_portfolio
    except Exception:
        return set()


# ── edge computation ──────────────────────────────────────────────────────────


def _compute_edge(pair: MarketPair) -> dict | None:
    """Compute arb edge for a market pair. Returns None if insufficient price data."""
    poly_ask = _poly_price(pair.poly.get('best_ask'))
    kalshi_ask = _kalshi_price(pair.kalshi.get('yes_ask'))

    if poly_ask is None or kalshi_ask is None:
        return None

    edge_poly_cheap = kalshi_ask - poly_ask
    edge_kalshi_cheap = poly_ask - kalshi_ask

    edge = max(edge_poly_cheap, edge_kalshi_cheap)
    edge_net = edge - _FEE_ESTIMATE * 2

    if edge_poly_cheap >= edge_kalshi_cheap:
        direction = 'buy_poly_yes_kalshi_no'
        rationale = (
            f'poly {float(poly_ask):.2f} vs kalshi {float(kalshi_ask):.2f}, '
            f'{float(edge)*100:.1f}% gap after fees'
        )
    else:
        direction = 'buy_kalshi_yes_poly_no'
        rationale = (
            f'kalshi {float(kalshi_ask):.2f} vs poly {float(poly_ask):.2f}, '
            f'{float(edge)*100:.1f}% gap after fees'
        )

    return {
        'poly_id': pair.poly.get('id', ''),
        'poly_question': pair.poly.get('question', '')[:80],
        'poly_token_id': pair.poly.get('token_id', ''),
        'kalshi_ticker': pair.kalshi.get('ticker', ''),
        'kalshi_event_ticker': pair.kalshi.get('event_ticker', ''),
        'poly_ask': str(poly_ask),
        'kalshi_ask': str(kalshi_ask),
        'similarity': pair.similarity,
        'edge': str(round(edge, 4)),
        'edge_net': str(round(edge_net, 4)),
        'direction': direction,
        'rationale': rationale,
        'already_in_portfolio': pair.already_in_portfolio,
    }


# ── event-sum scanning ───────────────────────────────────────────────────────


async def _fetch_event_sum_opportunities(
    query: str,
    limit: int,
    min_edge: Decimal,
    min_markets: int,
) -> list[dict]:
    """Fetch Polymarket events and compute sum(YES_ask) deviation for each."""
    import httpx

    from coinjure.cli.market_commands import GAMMA_EVENTS_URL

    async with httpx.AsyncClient(timeout=30.0) as client:
        params: dict = {
            'active': 'true',
            'closed': 'false',
            'limit': min(limit * 4, 200),
        }
        if query:
            params['tag_slug'] = query
        resp = await client.get(GAMMA_EVENTS_URL, params=params)
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket API error {resp.status_code}: {resp.text[:200]}'
        )

    events_raw = resp.json()
    q_lower = query.lower()

    _FEE = Decimal('0.005')
    opportunities: list[dict] = []

    for event in events_raw:
        title = event.get('title', '') or ''
        if query and q_lower not in title.lower():
            tags = [t.get('slug', '') for t in (event.get('tags') or [])]
            if not any(q_lower in tg for tg in tags):
                continue

        markets = event.get('markets') or []
        if len(markets) < min_markets:
            continue

        market_rows: list[dict] = []
        for m in markets:
            ask_str = m.get('bestAsk') or m.get('best_ask') or ''
            bid_str = m.get('bestBid') or m.get('best_bid') or ''
            try:
                ask = Decimal(str(ask_str)) if ask_str else None
            except Exception:
                ask = None
            try:
                bid = Decimal(str(bid_str)) if bid_str else None
            except Exception:
                bid = None

            from coinjure.cli.market_commands import _parse_clob_ids

            clob_ids = _parse_clob_ids(m)
            market_rows.append(
                {
                    'market_id': m.get('id', ''),
                    'question': (m.get('question', '') or '')[:80],
                    'token_id': clob_ids[0] if clob_ids else '',
                    'no_token_id': clob_ids[1] if len(clob_ids) > 1 else '',
                    'ask': str(ask) if ask is not None else None,
                    'bid': str(bid) if bid is not None else None,
                }
            )

        priced = [r for r in market_rows if r['ask'] is not None]
        if len(priced) < min_markets:
            continue

        sum_yes = sum(Decimal(r['ask']) for r in priced)
        n = len(priced)

        edge_buy_yes = Decimal('1') - sum_yes - _FEE * n
        edge_buy_no = sum_yes - Decimal('1') - _FEE * n
        best_edge = max(edge_buy_yes, edge_buy_no)

        if best_edge < min_edge:
            continue

        action = 'BUY_YES' if edge_buy_yes >= edge_buy_no else 'BUY_NO'
        opportunities.append(
            {
                'event_id': str(event.get('id', '')),
                'event_title': title[:80],
                'n_markets': n,
                'n_markets_total': len(markets),
                'sum_yes': str(round(sum_yes, 4)),
                'edge_buy_yes': str(round(edge_buy_yes, 4)),
                'edge_buy_no': str(round(edge_buy_no, 4)),
                'best_edge': str(round(best_edge, 4)),
                'action': action,
                'markets': priced,
            }
        )

    opportunities.sort(key=lambda x: Decimal(x['best_edge']), reverse=True)
    return opportunities[:limit]


# ── deployment helpers ────────────────────────────────────────────────────────


def _slug(text: str, max_len: int = 20) -> str:
    """Turn a string into a safe slug for use as a strategy-id component."""
    text = re.sub(r'[^a-z0-9]+', '-', text.lower())
    return text.strip('-')[:max_len]


def _deploy_one(
    opp: dict,
    strategy_ref: str,
    initial_capital: str,
    min_edge: float,
    trade_size: float,
    cooldown_seconds: int,
    hub_socket: str | None,
    dry_run: bool,
) -> dict:
    """Register + promote one opportunity. Returns a result dict."""
    poly_id = opp['poly_id']
    poly_token_id = opp.get('poly_token_id', '')
    kalshi_ticker = opp['kalshi_ticker']

    slug = _slug(opp.get('poly_question', poly_id))
    strategy_id = f'arb-{slug}-{poly_id[-6:]}'

    reg = StrategyRegistry(REGISTRY_PATH)

    if reg.get(strategy_id) is not None:
        return {
            'ok': True,
            'strategy_id': strategy_id,
            'skipped': True,
            'reason': 'already_in_registry',
        }

    kwargs: dict[str, Any] = {
        'poly_market_id': poly_id,
        'poly_token_id': poly_token_id,
        'kalshi_ticker': kalshi_ticker,
        'min_edge': min_edge,
        'trade_size': trade_size,
        'cooldown_seconds': cooldown_seconds,
    }

    try:
        from coinjure.strategy.loader import load_strategy_class

        cls = load_strategy_class(strategy_ref)
        cls(**kwargs)
    except Exception as exc:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': f'strategy validate failed: {exc}',
        }

    if dry_run:
        return {
            'ok': True,
            'strategy_id': strategy_id,
            'dry_run': True,
            'kwargs': kwargs,
        }

    entry = StrategyEntry(
        strategy_id=strategy_id,
        strategy_ref=strategy_ref,
        strategy_kwargs=kwargs,
        lifecycle='pending_backtest',
        exchange='cross_platform',
        data_dir=str(Path('data') / 'research' / strategy_id),
    )
    try:
        reg.add(entry)
    except ValueError as exc:
        return {'ok': False, 'strategy_id': strategy_id, 'error': str(exc)}

    coinjure_bin = shutil.which('coinjure')
    if not coinjure_bin:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': 'coinjure binary not found; activate the poetry virtualenv',
        }

    from coinjure.cli.control import SOCKET_DIR

    socket_path = SOCKET_DIR / f'{strategy_id}.sock'
    log_dir = Path('data') / 'research' / strategy_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'process.log'

    cmd = [
        coinjure_bin,
        'engine',
        'run',
        '--mode',
        'paper',
        '--exchange',
        'cross_platform',
        '--strategy-ref',
        strategy_ref,
        '--initial-capital',
        initial_capital,
        '--socket-path',
        str(socket_path),
        '--strategy-kwargs-json',
        json.dumps(kwargs),
    ]
    if hub_socket:
        cmd += ['--hub-socket', hub_socket]

    try:
        with open(log_file, 'a') as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
    except Exception as exc:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': f'launch failed: {exc}',
        }

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if socket_path.exists():
            break
        time.sleep(0.3)

    entry.lifecycle = 'paper_trading'
    entry.pid = proc.pid
    entry.socket_path = str(socket_path)
    reg.update(entry)

    return {
        'ok': True,
        'strategy_id': strategy_id,
        'pid': proc.pid,
        'socket': str(socket_path),
        'log': str(log_file),
        'kwargs': kwargs,
    }


def _deploy_event_sum_one(
    opp: dict,
    strategy_ref: str,
    initial_capital: str,
    min_edge: float,
    trade_size: float,
    cooldown_seconds: int,
    min_markets: int,
    hub_socket: str | None,
    dry_run: bool,
) -> dict:
    """Register + promote one EventSumArbStrategy instance."""
    event_id = opp['event_id']
    title_slug = _slug(opp.get('event_title', event_id))
    strategy_id = f'evtarb-{title_slug}-{event_id[-6:]}'

    reg = StrategyRegistry(REGISTRY_PATH)
    if reg.get(strategy_id) is not None:
        return {
            'ok': True,
            'strategy_id': strategy_id,
            'skipped': True,
            'reason': 'already_in_registry',
        }

    kwargs: dict[str, Any] = {
        'event_id': event_id,
        'min_edge': min_edge,
        'trade_size': trade_size,
        'cooldown_seconds': cooldown_seconds,
        'min_markets': min_markets,
    }

    try:
        from coinjure.strategy.loader import load_strategy_class

        cls = load_strategy_class(strategy_ref)
        cls(**kwargs)
    except Exception as exc:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': f'strategy validate failed: {exc}',
        }

    if dry_run:
        return {
            'ok': True,
            'strategy_id': strategy_id,
            'dry_run': True,
            'kwargs': kwargs,
        }

    entry = StrategyEntry(
        strategy_id=strategy_id,
        strategy_ref=strategy_ref,
        strategy_kwargs=kwargs,
        lifecycle='pending_backtest',
        exchange='polymarket',
        data_dir=str(Path('data') / 'research' / strategy_id),
    )
    try:
        reg.add(entry)
    except ValueError as exc:
        return {'ok': False, 'strategy_id': strategy_id, 'error': str(exc)}

    coinjure_bin = shutil.which('coinjure')
    if not coinjure_bin:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': 'coinjure binary not found',
        }

    from coinjure.cli.control import SOCKET_DIR

    socket_path = SOCKET_DIR / f'{strategy_id}.sock'
    log_dir = Path('data') / 'research' / strategy_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'process.log'

    cmd = [
        coinjure_bin,
        'engine',
        'run',
        '--mode',
        'paper',
        '--exchange',
        'polymarket',
        '--strategy-ref',
        strategy_ref,
        '--initial-capital',
        initial_capital,
        '--socket-path',
        str(socket_path),
        '--strategy-kwargs-json',
        json.dumps(kwargs),
    ]
    if hub_socket:
        cmd += ['--hub-socket', hub_socket]

    try:
        with open(log_file, 'a') as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
    except Exception as exc:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': f'launch failed: {exc}',
        }

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if socket_path.exists():
            break
        time.sleep(0.3)

    entry.lifecycle = 'paper_trading'
    entry.pid = proc.pid
    entry.socket_path = str(socket_path)
    reg.update(entry)

    return {
        'ok': True,
        'strategy_id': strategy_id,
        'pid': proc.pid,
        'socket': str(socket_path),
        'log': str(log_file),
        'kwargs': kwargs,
    }
