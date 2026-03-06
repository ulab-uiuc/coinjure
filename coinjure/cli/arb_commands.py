"""Arbitrage discovery CLI commands.

coinjure arb scan         — scan for live cross-platform arb opportunities
coinjure arb scan-events  — scan Polymarket events for intra-event sum arb
coinjure arb deploy       — scan + register + promote in one step (cross-platform)
coinjure arb deploy-events— scan events + deploy EventSumArbStrategy for each opportunity
coinjure market match     — fuzzy-match markets across platforms (added to market group)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click

from coinjure.cli.market_commands import (
    _kalshi_search_markets,
    _polymarket_search_markets,
)
from coinjure.portfolio.matching import MarketPair, match_markets
from coinjure.portfolio.registry import REGISTRY_PATH, StrategyEntry, StrategyRegistry

_DIRECT_ARB_REF = 'examples/strategies/direct_arb_strategy.py:DirectArbStrategy'
_EVENT_SUM_ARB_REF = 'examples/strategies/event_sum_arb_strategy.py:EventSumArbStrategy'

# Fee estimate: ~0.5% per side round-trip (conservative)
_FEE_ESTIMATE = Decimal('0.005')


# ── helpers ────────────────────────────────────────────────────────────────────


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


def _portfolio_ids() -> set[str]:
    """Return all strategy_ids currently in the portfolio (for already_in_portfolio)."""
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


def _compute_edge(pair: MarketPair) -> dict | None:
    """Compute arb edge for a market pair. Returns None if insufficient price data."""
    poly_ask = _poly_price(pair.poly.get('best_ask'))
    kalshi_ask = _kalshi_price(pair.kalshi.get('yes_ask'))

    if poly_ask is None or kalshi_ask is None:
        return None

    # Direction 1: Poly cheaper → buy Poly YES + buy Kalshi NO
    # cost = poly_ask + (1 - kalshi_ask), profit = 1 - cost = kalshi_ask - poly_ask
    edge_poly_cheap = kalshi_ask - poly_ask

    # Direction 2: Kalshi cheaper → buy Kalshi YES + buy Poly NO
    # cost = kalshi_ask + (1 - poly_ask), profit = poly_ask - kalshi_ask
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


# ── arb group ──────────────────────────────────────────────────────────────────


@click.group()
def arb() -> None:
    """Cross-platform arbitrage discovery commands."""


@arb.command('scan')
@click.option(
    '--query', required=True, help='Keyword to search markets on both platforms.'
)
@click.option(
    '--min-edge', default='0.02', show_default=True, help='Minimum gross edge (0-1).'
)
@click.option(
    '--min-similarity',
    default='0.6',
    show_default=True,
    help='Minimum fuzzy-match similarity.',
)
@click.option(
    '--limit',
    default=50,
    show_default=True,
    type=int,
    help='Max markets to fetch per exchange.',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def arb_scan(
    query: str,
    min_edge: str,
    min_similarity: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Scan for live cross-platform arb opportunities matching a keyword."""
    try:
        min_edge_dec = Decimal(min_edge)
        min_sim = float(min_similarity)
    except (InvalidOperation, ValueError) as exc:
        raise click.ClickException(f'Invalid numeric argument: {exc}') from exc

    async def _fetch_and_scan() -> list[dict]:
        poly_task = _polymarket_search_markets(query, limit)
        kalshi_task = _kalshi_search_markets(
            query, limit, kalshi_api_key_id, kalshi_private_key_path
        )
        poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        # Mark pairs already in portfolio.
        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        opportunities: list[dict] = []
        for pair in pairs:
            edge_info = _compute_edge(pair)
            if edge_info is None:
                continue
            if Decimal(edge_info['edge']) >= min_edge_dec:
                opportunities.append(edge_info)

        opportunities.sort(key=lambda x: Decimal(x['edge']), reverse=True)
        return opportunities

    try:
        opportunities = asyncio.run(_fetch_and_scan())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {'ok': True, 'query': query, 'opportunities': opportunities},
                default=str,
            )
        )
        return

    if not opportunities:
        click.echo(f'No arb opportunities found for {query!r} with edge >= {min_edge}.')
        return

    click.echo(f'\nArb scan: {query!r}  (min_edge={min_edge})\n')
    for opp in opportunities:
        already = ' [IN PORTFOLIO]' if opp['already_in_portfolio'] else ''
        click.echo(
            f'  edge={float(opp["edge"]):.3f} net={float(opp["edge_net"]):.3f}'
            f'  sim={opp["similarity"]:.2f}{already}'
        )
        click.echo(f'  poly:  {opp["poly_question"][:60]}')
        click.echo(f'  kalshi ticker: {opp["kalshi_ticker"]}')
        click.echo(f'  poly_ask={opp["poly_ask"]} kalshi_ask={opp["kalshi_ask"]}')
        click.echo(f'  {opp["rationale"]}')
        click.echo()


# ── arb deploy ─────────────────────────────────────────────────────────────────


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

    # Build a deterministic strategy_id from the pair
    slug = _slug(opp.get('poly_question', poly_id))
    strategy_id = f'arb-{slug}-{poly_id[-6:]}'

    reg = StrategyRegistry(REGISTRY_PATH)

    # Skip if already registered
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

    # --- Validate strategy can be instantiated ---
    try:
        from coinjure.strategy.loader import load_strategy_class

        cls = load_strategy_class(strategy_ref)
        cls(**kwargs)  # dry-instantiate
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

    # --- Register ---
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

    # --- Promote to paper_trading ---
    coinjure_bin = shutil.which('coinjure')
    if not coinjure_bin:
        return {
            'ok': False,
            'strategy_id': strategy_id,
            'error': 'coinjure binary not found; activate the poetry virtualenv',
        }

    import subprocess
    import time

    from coinjure.cli.control import SOCKET_DIR

    socket_path = SOCKET_DIR / f'{strategy_id}.sock'
    log_dir = Path('data') / 'research' / strategy_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'process.log'

    cmd = [
        coinjure_bin,
        'paper',
        'run',
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

    # Wait up to 5s for socket to appear
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if socket_path.exists():
            break
        time.sleep(0.3)

    # Update registry
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


@arb.command('deploy')
@click.option(
    '--query', required=True, help='Keyword to search markets on both platforms.'
)
@click.option(
    '--min-edge', default='0.02', show_default=True, help='Minimum gross edge (0-1).'
)
@click.option('--min-similarity', default='0.6', show_default=True)
@click.option('--limit', default=50, show_default=True, type=int)
@click.option(
    '--strategy-ref',
    default=_DIRECT_ARB_REF,
    show_default=True,
    help='Strategy class ref to deploy.',
)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option(
    '--trade-size',
    default=10.0,
    show_default=True,
    type=float,
    help='Dollar size per arb leg.',
)
@click.option('--cooldown-seconds', default=60, show_default=True, type=int)
@click.option(
    '--max-deploy',
    default=10,
    show_default=True,
    type=int,
    help='Maximum number of new strategies to deploy in this run.',
)
@click.option(
    '--hub-socket',
    default=None,
    type=click.Path(),
    help='Connect deployed strategies to a running Market Data Hub.',
)
@click.option(
    '--dry-run',
    is_flag=True,
    default=False,
    help='Scan and validate but do not actually register or launch processes.',
)
@click.option(
    '--skip-already-in-portfolio',
    is_flag=True,
    default=True,
    help='Skip opportunities already tracked in the portfolio.',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def arb_deploy(
    query: str,
    min_edge: str,
    min_similarity: str,
    limit: int,
    strategy_ref: str,
    initial_capital: str,
    trade_size: float,
    cooldown_seconds: int,
    max_deploy: int,
    hub_socket: str | None,
    dry_run: bool,
    skip_already_in_portfolio: bool,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Scan for arb opportunities and batch-deploy paper trading strategies.

    Combines arb scan + strategy validate + portfolio add + portfolio promote
    into a single command. The agent can call this without writing any code.

    Example:

        coinjure arb deploy --query "NBA" --min-edge 0.02 --max-deploy 5 --json
    """
    try:
        min_edge_dec = Decimal(min_edge)
        min_sim = float(min_similarity)
    except (InvalidOperation, ValueError) as exc:
        raise click.ClickException(f'Invalid numeric argument: {exc}') from exc

    # --- Step 1: Scan ---
    async def _scan() -> list[dict]:
        poly_markets, kalshi_markets = await asyncio.gather(
            _polymarket_search_markets(query, limit),
            _kalshi_search_markets(
                query, limit, kalshi_api_key_id, kalshi_private_key_path
            ),
        )
        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        opportunities: list[dict] = []
        for pair in pairs:
            edge_info = _compute_edge(pair)
            if edge_info is None:
                continue
            if Decimal(edge_info['edge']) >= min_edge_dec:
                opportunities.append(edge_info)

        opportunities.sort(key=lambda x: Decimal(x['edge']), reverse=True)
        return opportunities

    try:
        opportunities = asyncio.run(_scan())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    # Filter already-in-portfolio if requested
    to_deploy = [
        o
        for o in opportunities
        if not (skip_already_in_portfolio and o['already_in_portfolio'])
    ][:max_deploy]

    if not as_json:
        click.echo(
            f'Arb deploy: query={query!r}  found={len(opportunities)}  '
            f'to_deploy={len(to_deploy)}  dry_run={dry_run}'
        )

    # --- Step 2: Deploy each ---
    results: list[dict] = []
    for opp in to_deploy:
        result = _deploy_one(
            opp=opp,
            strategy_ref=strategy_ref,
            initial_capital=initial_capital,
            min_edge=float(min_edge_dec),
            trade_size=trade_size,
            cooldown_seconds=cooldown_seconds,
            hub_socket=hub_socket,
            dry_run=dry_run,
        )
        result['opportunity'] = {
            'poly_question': opp.get('poly_question', ''),
            'edge': opp['edge'],
            'edge_net': opp['edge_net'],
            'direction': opp['direction'],
        }
        results.append(result)
        if not as_json:
            status = (
                'DRY-RUN'
                if result.get('dry_run')
                else ('OK' if result['ok'] else 'FAIL')
            )
            click.echo(
                f'  [{status}] {result["strategy_id"]}  '
                f'edge={opp["edge"]}  {opp.get("poly_question", "")[:50]}'
            )
            if not result['ok']:
                click.echo(f'         error: {result.get("error")}')

    summary = {
        'ok': True,
        'query': query,
        'scanned': len(opportunities),
        'deployed': sum(
            1
            for r in results
            if r['ok'] and not r.get('skipped') and not r.get('dry_run')
        ),
        'skipped': sum(1 for r in results if r.get('skipped')),
        'failed': sum(1 for r in results if not r['ok']),
        'dry_run': dry_run,
        'results': results,
    }
    if as_json:
        click.echo(json.dumps(summary, default=str))


# ── arb scan-events ────────────────────────────────────────────────────────────


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
            params['tag_slug'] = query  # broad filter; we also filter by title below
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
        # Filter by query keyword in title or tag
        if query and q_lower not in title.lower():
            tags = [t.get('slug', '') for t in (event.get('tags') or [])]
            if not any(q_lower in tg for tg in tags):
                continue

        markets = event.get('markets') or []
        # Only multi-outcome events
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

        # Compute sum only over markets that have ask prices
        priced = [r for r in market_rows if r['ask'] is not None]
        if len(priced) < min_markets:
            continue

        sum_yes = sum(Decimal(r['ask']) for r in priced)
        n = len(priced)

        edge_buy_yes = Decimal('1') - sum_yes - _FEE * n  # buy all YES
        edge_buy_no = sum_yes - Decimal('1') - _FEE * n  # buy all NO
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


@arb.command('scan-events')
@click.option(
    '--query', default='', help='Keyword to filter event titles (empty = all).'
)
@click.option(
    '--min-edge',
    default='0.01',
    show_default=True,
    help='Minimum net edge after fees (0-1).',
)
@click.option(
    '--min-markets',
    default=2,
    show_default=True,
    type=int,
    help='Minimum number of priced markets in the event.',
)
@click.option(
    '--limit',
    default=20,
    show_default=True,
    type=int,
    help='Max opportunities to return.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def arb_scan_events(
    query: str,
    min_edge: str,
    min_markets: int,
    limit: int,
    as_json: bool,
) -> None:
    """Scan Polymarket events for intra-event sum(YES) arbitrage.

    Finds multi-outcome events where the sum of YES ask prices deviates
    from 1.0 by more than fees, creating a risk-free arb opportunity.

    Example:

        coinjure arb scan-events --query "NBA" --min-edge 0.01 --json
    """
    try:
        min_edge_dec = Decimal(min_edge)
    except InvalidOperation as exc:
        raise click.ClickException(f'Invalid --min-edge: {exc}') from exc

    try:
        opportunities = asyncio.run(
            _fetch_event_sum_opportunities(query, limit * 4, min_edge_dec, min_markets)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    opportunities = opportunities[:limit]

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'query': query,
                    'count': len(opportunities),
                    'opportunities': opportunities,
                },
                default=str,
            )
        )
        return

    if not opportunities:
        click.echo(
            f'No event-sum arb opportunities found (query={query!r}, min_edge={min_edge}).'
        )
        return

    click.echo(f'\nEvent-sum arb scan  query={query!r}  min_edge={min_edge}\n')
    for opp in opportunities:
        click.echo(
            f'  edge={opp["best_edge"]}  action={opp["action"]}'
            f'  n={opp["n_markets"]}  sum_yes={opp["sum_yes"]}'
        )
        click.echo(f'  event: {opp["event_title"]}')
        click.echo(f'  event_id: {opp["event_id"]}')
        click.echo()


# ── arb deploy-events ──────────────────────────────────────────────────────────


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

    # Validate instantiation
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

    import subprocess
    import time

    from coinjure.cli.control import SOCKET_DIR

    socket_path = SOCKET_DIR / f'{strategy_id}.sock'
    log_dir = Path('data') / 'research' / strategy_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'process.log'

    cmd = [
        coinjure_bin,
        'paper',
        'run',
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


@arb.command('deploy-events')
@click.option(
    '--query', default='', help='Keyword to filter event titles (empty = all).'
)
@click.option('--min-edge', default='0.01', show_default=True)
@click.option('--min-markets', default=2, show_default=True, type=int)
@click.option(
    '--limit', default=20, show_default=True, type=int, help='Max events to scan.'
)
@click.option('--strategy-ref', default=_EVENT_SUM_ARB_REF, show_default=True)
@click.option('--initial-capital', default='10000', show_default=True)
@click.option('--trade-size', default=10.0, show_default=True, type=float)
@click.option('--cooldown-seconds', default=120, show_default=True, type=int)
@click.option('--max-deploy', default=10, show_default=True, type=int)
@click.option('--hub-socket', default=None, type=click.Path())
@click.option('--dry-run', is_flag=True, default=False)
@click.option('--json', 'as_json', is_flag=True, default=False)
def arb_deploy_events(
    query: str,
    min_edge: str,
    min_markets: int,
    limit: int,
    strategy_ref: str,
    initial_capital: str,
    trade_size: float,
    cooldown_seconds: int,
    max_deploy: int,
    hub_socket: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Scan Polymarket event-sum arb + batch-deploy EventSumArbStrategy.

    Example:

        coinjure arb deploy-events --query "NBA" --min-edge 0.01 --max-deploy 5 --json
    """
    try:
        min_edge_dec = Decimal(min_edge)
    except InvalidOperation as exc:
        raise click.ClickException(f'Invalid --min-edge: {exc}') from exc

    try:
        opportunities = asyncio.run(
            _fetch_event_sum_opportunities(query, limit, min_edge_dec, min_markets)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Scan failed: {exc}') from exc

    to_deploy = opportunities[:max_deploy]

    if not as_json:
        click.echo(
            f'deploy-events: query={query!r}  found={len(opportunities)}  '
            f'to_deploy={len(to_deploy)}  dry_run={dry_run}'
        )

    results: list[dict] = []
    for opp in to_deploy:
        result = _deploy_event_sum_one(
            opp=opp,
            strategy_ref=strategy_ref,
            initial_capital=initial_capital,
            min_edge=float(min_edge_dec),
            trade_size=trade_size,
            cooldown_seconds=cooldown_seconds,
            min_markets=min_markets,
            hub_socket=hub_socket,
            dry_run=dry_run,
        )
        result['opportunity'] = {
            'event_title': opp.get('event_title', ''),
            'event_id': opp['event_id'],
            'best_edge': opp['best_edge'],
            'action': opp['action'],
            'n_markets': opp['n_markets'],
            'sum_yes': opp['sum_yes'],
        }
        results.append(result)
        if not as_json:
            status = (
                'DRY-RUN'
                if result.get('dry_run')
                else ('OK' if result['ok'] else 'FAIL')
            )
            click.echo(
                f'  [{status}] {result["strategy_id"]}'
                f'  edge={opp["best_edge"]}  {opp.get("event_title", "")[:50]}'
            )
            if not result['ok']:
                click.echo(f'         error: {result.get("error")}')

    summary = {
        'ok': True,
        'query': query,
        'scanned': len(opportunities),
        'deployed': sum(
            1
            for r in results
            if r['ok'] and not r.get('skipped') and not r.get('dry_run')
        ),
        'skipped': sum(1 for r in results if r.get('skipped')),
        'failed': sum(1 for r in results if not r['ok']),
        'dry_run': dry_run,
        'results': results,
    }
    if as_json:
        click.echo(json.dumps(summary, default=str))


# ── market match command (registered into market group externally) ─────────────


@click.command('match')
@click.option('--query', required=True, help='Keyword to search on both platforms.')
@click.option('--min-similarity', default='0.6', show_default=True)
@click.option('--limit', default=50, show_default=True, type=int)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_match_cmd(
    query: str,
    min_similarity: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Fuzzy-match markets across Polymarket and Kalshi by keyword."""
    try:
        min_sim = float(min_similarity)
    except ValueError as exc:
        raise click.ClickException(f'Invalid --min-similarity: {exc}') from exc

    async def _fetch() -> list[MarketPair]:
        poly_markets, kalshi_markets = await asyncio.gather(
            _polymarket_search_markets(query, limit),
            _kalshi_search_markets(
                query, limit, kalshi_api_key_id, kalshi_private_key_path
            ),
        )
        pairs = match_markets(poly_markets, kalshi_markets, min_similarity=min_sim)

        in_portfolio = _pair_ids_in_portfolio(pairs)
        for pair in pairs:
            key = f'{pair.poly.get("id", "")}::{pair.kalshi.get("ticker", "")}'
            pair.already_in_portfolio = key in in_portfolio

        return pairs

    try:
        pairs = asyncio.run(_fetch())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to match markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'query': query,
                    'count': len(pairs),
                    'pairs': [
                        {
                            'poly': p.poly,
                            'kalshi': p.kalshi,
                            'similarity': p.similarity,
                            'already_in_portfolio': p.already_in_portfolio,
                        }
                        for p in pairs
                    ],
                },
                default=str,
            )
        )
        return

    if not pairs:
        click.echo(f'No matching market pairs found for {query!r}.')
        return

    click.echo(
        f'\nMarket pairs matching {query!r}  (min_similarity={min_similarity}):\n'
    )
    for p in pairs:
        already = ' [IN PORTFOLIO]' if p.already_in_portfolio else ''
        click.echo(f'  sim={p.similarity:.3f}{already}')
        click.echo(f'  poly:   {p.poly.get("question", "")[:70]}')
        click.echo(
            f'  kalshi: {p.kalshi.get("title", "")[:70]}  [{p.kalshi.get("ticker", "")}]'
        )
        click.echo()
