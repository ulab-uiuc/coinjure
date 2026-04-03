"""CLI commands for browsing prediction markets on Polymarket and Kalshi."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import click

from coinjure.data.fetcher import (
    kalshi_list_markets,
    kalshi_market_info,
    kalshi_search_markets,
    polymarket_list_markets,
    polymarket_market_info,
    polymarket_search_markets,
)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _box_table(
    headers: list[str],
    rows: list[list[str]],
    alignments: list[str] | None = None,
    indent: int = 2,
) -> str:
    """Render a table with Unicode box-drawing borders.

    *alignments* is a list of ``'<'`` (left) or ``'>'`` (right) per column.
    Defaults to left-aligned.
    """
    n_cols = len(headers)
    if alignments is None:
        alignments = ['<'] * n_cols

    # Compute column widths (header vs data)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < n_cols:
                widths[i] = max(widths[i], len(cell))

    pad = ' ' * indent

    def _sep(left: str, mid: str, right: str, fill: str = '─') -> str:
        return pad + left + mid.join(fill * (w + 2) for w in widths) + right

    def _row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            w = widths[i]
            if alignments[i] == '>':
                parts.append(f' {cell:>{w}} ')
            else:
                parts.append(f' {cell:<{w}} ')
        return pad + '│' + '│'.join(parts) + '│'

    lines: list[str] = []
    lines.append(_sep('┌', '┬', '┐'))
    lines.append(_row(headers))
    lines.append(_sep('├', '┼', '┤'))
    for row in rows:
        # Pad row to n_cols if short
        padded = list(row) + [''] * (n_cols - len(row))
        lines.append(_row(padded))
    lines.append(_sep('└', '┴', '┘'))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def market() -> None:
    """Explore prediction markets on Polymarket and Kalshi."""


# ---------------------------------------------------------------------------
# market info (fetch detailed market information)
# ---------------------------------------------------------------------------


@market.command('info')
@click.option('--market-id', default=None, help='Market ID or ticker.')
@click.option(
    '--slug',
    default=None,
    help='Polymarket event slug from URL (e.g. fed-decision-in-april).',
)
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi']),
    default='polymarket',
    show_default=True,
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def market_info(
    market_id: str | None,
    slug: str | None,
    exchange: str,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Fetch detailed information for a single market or all markets in an event (by slug)."""
    if not market_id and not slug:
        raise click.UsageError('Provide --market-id or --slug.')

    # Slug-based: list all markets in the event
    if slug:
        from coinjure.data.fetcher import polymarket_fetch_by_slug

        markets = asyncio.run(polymarket_fetch_by_slug(slug, with_rules=True))
        if not markets:
            raise click.ClickException(f'No markets found for slug: {slug}')
        if as_json:
            click.echo(
                json.dumps(
                    {'ok': True, 'exchange': 'polymarket', 'markets': markets},
                    default=str,
                )
            )
            return
        click.echo(f'\n  Polymarket Event: {slug}  ({len(markets)} market(s))\n')
        rows: list[list[str]] = []
        for m in markets:
            mid = m.get('id', '')
            bid = m.get('best_bid', '')
            ask = m.get('best_ask', '')
            vol = m.get('volume', '')
            q = m.get('question', '')[:48]
            try:
                bid_s = f'{float(bid):.3f}' if bid not in (None, '') else '—'
            except (ValueError, TypeError):
                bid_s = '—'
            try:
                ask_s = f'{float(ask):.3f}' if ask not in (None, '') else '—'
            except (ValueError, TypeError):
                ask_s = '—'
            try:
                vol_s = f'{float(str(vol).replace(",", "")):.0f}'
            except (ValueError, TypeError):
                vol_s = '—'
            rows.append([mid, bid_s, ask_s, vol_s, q])
        click.echo(
            _box_table(
                ['ID', 'Bid', 'Ask', 'Volume', 'Question'],
                rows,
                ['>', '>', '>', '>', '<'],
            )
        )
        click.echo()
        return

    if market_id is None:
        raise click.UsageError('--market-id is required without --slug.')

    try:
        if exchange == 'polymarket':
            info = asyncio.run(polymarket_market_info(market_id))
        else:
            info = asyncio.run(
                kalshi_market_info(
                    market_id, kalshi_api_key_id, kalshi_private_key_path
                )
            )
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch market: {exc}') from exc
    if info is None:
        raise click.ClickException(f'Market not found: {market_id}')

    result: dict[str, Any] = {'ok': True, 'exchange': exchange, **info}

    if as_json:
        click.echo(json.dumps(result, default=str))
        return

    question = info.get('question') or info.get('title') or '?'
    mid = market_id or info.get('id') or info.get('ticker', '')
    bid = info.get('best_bid') or info.get('yes_bid', '')
    ask = info.get('best_ask') or info.get('yes_ask', '')
    vol = info.get('volume', '')
    end = info.get('end_date') or info.get('close_time', '')
    event = info.get('event_id') or info.get('event_ticker', '')
    status = info.get('status', '')
    active = info.get('active')
    closed = info.get('closed')
    tokens = info.get('token_ids', [])

    click.echo(f'\n  {question}\n')

    # Key-value info table
    kv_rows: list[list[str]] = [
        ['Exchange', exchange],
        ['Market ID', str(mid)],
    ]
    if event:
        kv_rows.append(['Event ID', str(event)])
    click.echo(_box_table(['Field', 'Value'], kv_rows))
    click.echo()

    # Price / volume table
    try:
        bid_s = f'{float(bid):.4f}' if bid not in (None, '') else '—'
    except (ValueError, TypeError):
        bid_s = '—'
    try:
        ask_s = f'{float(ask):.4f}' if ask not in (None, '') else '—'
    except (ValueError, TypeError):
        ask_s = '—'
    try:
        vol_s = (
            f'{float(str(vol).replace(",", "")):,.0f}' if vol not in (None, '') else '—'
        )
    except (ValueError, TypeError):
        vol_s = '—'
    click.echo(
        _box_table(
            ['Bid', 'Ask', 'Volume'],
            [[bid_s, ask_s, vol_s]],
            ['>', '>', '>'],
        )
    )
    click.echo()

    # Status details table
    detail_rows: list[list[str]] = []
    if end:
        detail_rows.append(['End Date', str(end)])
    if status:
        detail_rows.append(['Status', str(status)])
    if active is not None:
        detail_rows.append(['Active', str(active)])
    if closed is not None:
        detail_rows.append(['Closed', str(closed)])
    if tokens:
        detail_rows.append(['Token IDs', ', '.join(str(t) for t in tokens)])
    if detail_rows:
        click.echo(_box_table(['Field', 'Value'], detail_rows))

    desc = info.get('description') or info.get('rules_primary', '')
    if desc:
        click.echo(f'\n  Description:')
        for i in range(0, min(len(desc), 300), 76):
            click.echo(f'    {desc[i:i + 76]}')
        if len(desc) > 300:
            click.echo('    ...')
    click.echo()


# ---------------------------------------------------------------------------
# market relations (manage the persisted relation graph)
# ---------------------------------------------------------------------------


@market.group('relations')
def market_relations_group() -> None:
    """Manage the persisted market relation graph."""


def _detect_exchange(market_id: str) -> str:
    """Auto-detect exchange from market ID: numeric → polymarket, else → kalshi."""
    return 'polymarket' if market_id.isdigit() else 'kalshi'


@market_relations_group.command('add')
@click.option(
    '--market-id',
    '-m',
    'market_ids',
    required=True,
    multiple=True,
    help='Market ID (repeatable, >=2 required). e.g. -m 610380 -m 610381 -m 610382',
)
@click.option(
    '--exchange',
    'exc',
    type=click.Choice(['polymarket', 'kalshi', 'auto']),
    default='auto',
    show_default=True,
    help='Exchange for all markets. Use "auto" to detect per market ID (numeric=polymarket, else=kalshi).',
)
@click.option(
    '--spread-type',
    default='correlated',
    show_default=True,
    help='Relation type (same_event, complementary, implication, exclusivity, correlated, structural, conditional, temporal).',
)
@click.option('--hypothesis', default='', help='Price relationship hypothesis.')
@click.option('--reasoning', default='', help='Why these markets are related.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_add(
    market_ids: tuple[str, ...],
    exc: str,
    spread_type: str,
    hypothesis: str,
    reasoning: str,
    as_json: bool,
) -> None:
    """Create a relation between markets (group of 2+).

    Fetches market info from the API and persists the relation.

    \b
      coinjure market relations add \\
        -m 553856 -m 553860 -m 553875 \\
        --spread-type exclusivity \\
        --hypothesis "sum(prices) <= 1"
    """
    from coinjure.market.relations import MarketRelation, RelationStore

    if len(market_ids) < 2:
        raise click.ClickException('At least 2 market IDs required.')

    async def _fetch_info(mid: str, platform: str) -> dict:
        if platform == 'polymarket':
            info = await polymarket_market_info(mid)
        else:
            info = await kalshi_market_info(
                mid,
                api_key_id=None,
                private_key_path=None,
            )
        if info is None:
            raise click.ClickException(f'Market not found: {mid}')
        info['platform'] = platform
        return info

    infos: list[dict] = []
    try:
        for mid in market_ids:
            platform = exc if exc != 'auto' else _detect_exchange(mid)
            infos.append(asyncio.run(_fetch_info(mid, platform)))
    except Exception as exc_err:
        raise click.ClickException(
            f'Failed to fetch market info: {exc_err}'
        ) from exc_err

    # Build relation ID: first 3 IDs + overflow count
    sorted_ids = sorted(mid[:8] for mid in market_ids)
    rid = '-'.join(sorted_ids[:3])
    if len(sorted_ids) > 3:
        rid += f'-+{len(sorted_ids) - 3}'

    rel = MarketRelation(
        relation_id=rid,
        markets=infos,
        spread_type=spread_type,
        reasoning=reasoning,
        hypothesis=hypothesis,
    )

    store = RelationStore()
    store.add(rel)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'relation_id': rid,
                    'relation': rel.to_dict(),
                },
                default=str,
            )
        )
        return

    click.echo(f'\n  Relation created: {rid}')
    click.echo(f'  Type: {spread_type}  |  Markets: {len(infos)}')
    if hypothesis:
        click.echo(f'  Hypothesis: {hypothesis}')
    click.echo()
    rows = [
        [str(i), info.get('platform', '?'), info.get('question', '')[:50]]
        for i, info in enumerate(infos)
    ]
    click.echo(_box_table(['#', 'Platform', 'Question'], rows, ['>', '<', '<']))
    click.echo()


@market_relations_group.command('list')
@click.option('--type', 'spread_type', default=None, help='Filter by spread type.')
@click.option(
    '--status',
    default=None,
    help='Filter by status (active, deployed, invalidated, retired).',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_list(
    spread_type: str | None,
    status: str | None,
    as_json: bool,
) -> None:
    """List all stored market relations."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    relations = store.list(spread_type=spread_type, status=status)

    if as_json:
        click.echo(
            json.dumps(
                {
                    'ok': True,
                    'count': len(relations),
                    'relations': [r.to_dict() for r in relations],
                },
                default=str,
            )
        )
        return

    if not relations:
        click.echo('No relations found.')
        return

    click.echo(f'\n  {len(relations)} market relation(s)')
    filters = []
    if spread_type:
        filters.append(f'type={spread_type}')
    if status:
        filters.append(f'status={status}')
    if filters:
        click.echo(f'  Filters: {", ".join(filters)}')
    click.echo()

    # Summary table
    summary_rows = [
        [
            r.relation_id,
            r.spread_type,
            r.status,
            f'{r.confidence:.2f}',
            str(len(r.markets)),
        ]
        for r in relations
    ]
    click.echo(
        _box_table(
            ['Relation ID', 'Type', 'Status', 'Conf', 'Mkts'],
            summary_rows,
            ['<', '<', '<', '>', '>'],
        )
    )
    click.echo()

    # Detail section
    for r in relations:
        click.echo(f'  {r.relation_id}')
        for i, m in enumerate(r.markets):
            plat = m.get('platform', '?')
            q = m.get('question', '?')[:60]
            click.echo(f'    [{i}] {plat:<12} {q}')
        if r.hypothesis:
            click.echo(f'    Hypothesis: {r.hypothesis}')
        click.echo()


@market_relations_group.command('remove')
@click.argument('relation_id')
def relations_remove(relation_id: str) -> None:
    """Remove a relation from the store."""
    from coinjure.market.relations import RelationStore

    store = RelationStore()
    if store.remove(relation_id):
        click.echo(f'Removed relation: {relation_id}')
    else:
        raise click.ClickException(f'Relation not found: {relation_id}')


# ---------------------------------------------------------------------------
# market discover (multi-keyword search + structural relation detection)
# ---------------------------------------------------------------------------


@market.command('discover')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'both']),
    default='both',
    show_default=True,
)
@click.option(
    '--query',
    '-q',
    multiple=True,
    help='Search keywords (repeatable). Searches each keyword on each exchange '
    'and merges unique results. If omitted, fetches top markets by volume.',
)
@click.option(
    '--tag',
    '-t',
    multiple=True,
    help='Polymarket event tags to filter by (repeatable). '
    'e.g. -t Crypto -t Finance. Merged with --query results.',
)
@click.option(
    '--limit',
    default=100,
    show_default=True,
    type=int,
    help='Markets to fetch per exchange (or per query when --query is used).',
)
@click.option('--kalshi-api-key-id', default=None, envvar='KALSHI_API_KEY_ID')
@click.option(
    '--kalshi-private-key-path', default=None, envvar='KALSHI_PRIVATE_KEY_PATH'
)
@click.option(
    '--with-rules/--no-rules',
    'with_rules',
    default=True,
    show_default=True,
    help='Include resolution rules/description. Use --no-rules to omit.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
@click.option(
    '--auto-discover/--no-auto-discover',
    'auto_discover',
    default=True,
    show_default=True,
    help='Auto-detect and persist intra-event structural relations '
    '(implication, exclusivity, complementary). Use --no-auto-discover to disable.',
)
def market_discover(
    exchange: str,
    query: tuple[str, ...],
    tag: tuple[str, ...],
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    with_rules: bool,
    as_json: bool,
    auto_discover: bool,
) -> None:
    """Fetch markets from exchanges for agent analysis.

    Searches multiple keywords and/or tags, merges unique markets, and returns
    the raw data. Automatically detects intra-event structural relations
    (date nesting, verb implication, exclusivity, complementary). Cross-event
    and cross-platform semantic relations are left for the agent to decide
    via ``market relations add``.

    \b
      coinjure market discover -q "election" -q "Trump" --json
      coinjure market discover -q "Trump resign" --exchange both --with-rules --json
      coinjure market discover -t Crypto -t Finance --exchange polymarket --json
    """

    # ── Fetch markets ──────────────────────────────────────────────────

    async def _fetch_markets() -> tuple[list[dict], list[dict]]:
        # Fetch all matching markets — limit is only applied at output time.
        _NO_LIMIT = 10_000

        poly_markets: list[dict] = []
        kalshi_markets: list[dict] = []
        poly_seen: set[str] = set()
        kalshi_seen: set[str] = set()

        def _merge_poly(results: list[dict]) -> None:
            for mk in results:
                mid = mk.get('id', '')
                if mid and mid not in poly_seen:
                    poly_seen.add(mid)
                    poly_markets.append(mk)

        def _merge_kalshi(results: list[dict]) -> None:
            for mk in results:
                mid = mk.get('ticker', '')
                if mid and mid not in kalshi_seen:
                    kalshi_seen.add(mid)
                    kalshi_markets.append(mk)

        if query or tag:
            for q in query:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(
                        await polymarket_search_markets(
                            q,
                            _NO_LIMIT,
                            with_rules=with_rules,
                        )
                    )
                if exchange in ('kalshi', 'both'):
                    _merge_kalshi(
                        await kalshi_search_markets(
                            q,
                            _NO_LIMIT,
                            kalshi_api_key_id,
                            kalshi_private_key_path,
                            with_rules=with_rules,
                        )
                    )
            for t in tag:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(
                        await polymarket_list_markets(
                            _NO_LIMIT,
                            tag=t,
                            with_rules=with_rules,
                        )
                    )
        else:
            if exchange in ('polymarket', 'both'):
                poly_markets = await polymarket_list_markets(
                    _NO_LIMIT,
                    with_rules=with_rules,
                )
            if exchange in ('kalshi', 'both'):
                kalshi_markets = await kalshi_list_markets(
                    _NO_LIMIT,
                    kalshi_api_key_id,
                    kalshi_private_key_path,
                )

        return poly_markets, kalshi_markets

    try:
        poly_markets, kalshi_markets = asyncio.run(_fetch_markets())
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch markets: {exc}') from exc

    if not poly_markets and not kalshi_markets:
        raise click.ClickException('No markets fetched. Check API keys or queries.')

    # ── Filter zombie markets (no bid AND no ask) ─────────────────────

    def _is_alive(m: dict) -> bool:
        """Require both bid > 0 and ask > 0 — zombie markets have one or both missing."""
        bid = m.get('best_bid') or m.get('yes_bid', '')
        ask = m.get('best_ask') or m.get('yes_ask', '')
        try:
            return (float(bid) > 0 if bid else False) and (
                float(ask) > 0 if ask else False
            )
        except (ValueError, TypeError):
            return False

    # Filter zombies but don't limit yet — auto-discover needs full pool
    poly_alive = [m for m in poly_markets if _is_alive(m)]
    kalshi_alive = [m for m in kalshi_markets if _is_alive(m)]

    # ── Annotate with existing relations ──────────────────────────────

    from coinjure.market.relations import RelationStore

    store = RelationStore()
    all_relations = store.list()

    # Build a set of market IDs already in relations
    related_ids: dict[str, list[str]] = {}  # market_id → [relation_id, ...]
    for r in all_relations:
        for m in r.markets:
            for key in ('id', 'market_id', 'ticker'):
                mid = m.get(key, '')
                if mid:
                    related_ids.setdefault(mid, []).append(r.relation_id)

    # Tag each market with its existing relations
    for mk in poly_alive:
        mid = mk.get('id', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]
    for mk in kalshi_alive:
        mid = mk.get('ticker', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]

    # Compact summary of existing relations for agent context
    relation_summary = [
        {
            'relation_id': r.relation_id,
            'type': r.spread_type,
            'status': r.status,
            'market_count': len(r.markets),
            'market_ids': [m.get('id', m.get('ticker', '')) for m in r.markets],
            'market_questions': [m.get('question', '')[:60] for m in r.markets],
        }
        for r in all_relations
    ]

    # ── Auto-discover ──────────────────────────────────────────────────

    auto_discover_summary: dict[str, Any] | None = None
    if auto_discover:
        from coinjure.market.auto_discover import discover_relations

        # Use full pool for discovery, not limited
        result = discover_relations(poly_alive, kalshi_alive)

        # Persist candidates to relation store
        stored_new = 0
        if result.candidates:
            from coinjure.market.relations import RelationStore

            store = RelationStore()
            stored_new = store.add_batch(result.candidates)

        auto_discover_summary = {
            'total_detected': result.total_detected,
            'candidate_count': len(result.candidates),
            'stored_new': stored_new,
            'by_type': result.by_type,
            'by_layer': result.by_layer,
            'candidates': [
                {
                    'relation_id': r.relation_id,
                    'type': r.spread_type,
                    'confidence': r.confidence,
                    'reasoning': r.reasoning,
                    'market_count': len(r.markets),
                    'market_ids': [m.get('id', '') for m in r.markets],
                    'market_questions': [m.get('question', '')[:60] for m in r.markets],
                }
                for r in result.candidates
            ],
        }

    # ── Apply limit for output (discovery used full pool above) ─────
    poly_out = poly_alive[:limit]
    kalshi_out = kalshi_alive[:limit]

    # ── Output ─────────────────────────────────────────────────────────

    total = len(poly_out) + len(kalshi_out)
    total_alive = len(poly_alive) + len(kalshi_alive)

    if as_json:
        payload: dict[str, Any] = {
            'ok': True,
            'markets': {
                'polymarket': poly_out,
                'kalshi': kalshi_out,
            },
            'total_markets': total,
            'total_alive': total_alive,
            'existing_relations': relation_summary,
            'existing_relation_count': len(relation_summary),
        }
        if auto_discover_summary is not None:
            payload['auto_discover'] = auto_discover_summary
        click.echo(json.dumps(payload, default=str))
        return

    parts = [f'"{q}"' for q in query] + [f'tag:{t}' for t in tag]
    query_desc = ', '.join(parts) if parts else 'top by volume'
    click.echo(
        f'\nDiscovered {len(poly_alive)} Polymarket + {len(kalshi_alive)} Kalshi '
        f'alive markets (queries: {query_desc})'
    )
    if total_alive > total:
        click.echo(f'  (showing {total} of {total_alive}, use --limit to show more)')
    click.echo()

    def _fmt_table(
        markets: list[dict],
        platform: str,
    ) -> None:
        if not markets:
            return
        click.echo(f'  [{platform}] {len(markets)} markets\n')
        rows: list[list[str]] = []
        for m in markets:
            mid = m.get('id') or m.get('ticker', '')
            bid = m.get('best_bid') or m.get('yes_bid', '')
            ask = m.get('best_ask') or m.get('yes_ask', '')
            vol = m.get('volume', 0)
            q = (m.get('question') or m.get('title', ''))[:50]
            try:
                bid_s = f'{float(bid):.3f}' if bid not in (None, '') else '—'
            except (ValueError, TypeError):
                bid_s = '—'
            try:
                ask_s = f'{float(ask):.3f}' if ask not in (None, '') else '—'
            except (ValueError, TypeError):
                ask_s = '—'
            try:
                vol_s = f'{float(str(vol).replace(",", "")):.0f}'
            except (ValueError, TypeError):
                vol_s = '0'
            rows.append([mid, bid_s, ask_s, vol_s, q])
        click.echo(
            _box_table(
                ['ID', 'Bid', 'Ask', 'Volume', 'Question'],
                rows,
                ['>', '>', '>', '>', '<'],
            )
        )
        click.echo()

    _fmt_table(poly_out, 'Polymarket')
    _fmt_table(kalshi_out, 'Kalshi')

    if auto_discover_summary:
        s = auto_discover_summary
        td = s['total_detected']
        cc = s['candidate_count']
        sn = s.get('stored_new', 0)
        click.echo(f'  Auto-discover: {td} relations detected, {cc} candidates')
        if cc > 0:
            click.echo(f'    Stored {sn} new relation(s) ({cc - sn} already in store)')
        if s['by_type']:
            click.echo(f'    By type: {s["by_type"]}')
        click.echo()
        candidates: list[dict[str, Any]] = s.get('candidates', [])
        for cand in candidates[:20]:
            n = cand.get('market_count', 0)
            ids = cand.get('market_ids', [])
            ids_s = ', '.join(str(i) for i in ids[:3])
            if len(ids) > 3:
                ids_s += f' +{len(ids)-3} more'
            click.echo(f'    [{cand["type"]}] {n} markets: {ids_s}')
            for q in cand.get('market_questions', [])[:5]:
                click.echo(f'      - {q}')
            click.echo(f'      {cand["reasoning"]}')
            click.echo()
        if len(candidates) > 20:
            click.echo(f'    ... and {len(s["candidates"]) - 20} more')
            click.echo()


# ---------------------------------------------------------------------------
# market news (absorbed from news_commands.py)
# ---------------------------------------------------------------------------


@market.command('news')
@click.option(
    '--source',
    type=click.Choice(['google', 'rss', 'thenewsapi']),
    default='google',
    show_default=True,
    help='News source to fetch from.',
)
@click.option('--query', default=None, help='Optional search/filter query.')
@click.option(
    '--limit', default=10, show_default=True, type=int, help='Max articles to fetch.'
)
@click.option(
    '--api-token',
    default=None,
    help='TheNewsAPI token (or THENEWSAPI_TOKEN env var). Required for --source thenewsapi.',
)
@click.option('--json', 'as_json', is_flag=True, default=False, help='Output as JSON.')
def market_news(
    source: str, query: str | None, limit: int, api_token: str | None, as_json: bool
) -> None:
    """Fetch news headlines from a specified source."""
    from coinjure.data.news import (
        fetch_google_news,
        fetch_rss,
        fetch_thenewsapi,
    )

    token = api_token or os.environ.get('THENEWSAPI_TOKEN', '')

    try:
        if source == 'google':
            articles = asyncio.run(fetch_google_news(query, limit))
        elif source == 'rss':
            articles = asyncio.run(fetch_rss(query, limit))
        else:
            if not token:
                raise click.ClickException(
                    'TheNewsAPI requires a token. Pass --api-token or set THENEWSAPI_TOKEN.'
                )
            articles = asyncio.run(fetch_thenewsapi(query, limit, token))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch news: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps({'source': source, 'count': len(articles), 'articles': articles})
        )
        return

    if not articles:
        click.echo('No articles found.')
        return

    click.echo(f'\n  {len(articles)} article(s) from {source}')
    if query:
        click.echo(f'  Query: "{query}"')
    click.echo()
    rows: list[list[str]] = []
    for i, article in enumerate(articles, 1):
        title = article.get('title', '(no title)')[:50]
        src = article.get('source', '—')[:16]
        pub = article.get('published_at', '')[:19] or '—'
        rows.append([str(i), pub, src, title])
    click.echo(
        _box_table(
            ['#', 'Published', 'Source', 'Title'],
            rows,
            ['>', '<', '<', '<'],
        )
    )
    click.echo()
    # Detail section
    for i, article in enumerate(articles, 1):
        url = article.get('url', '')
        desc = article.get('description', '')
        if url or desc:
            title = article.get('title', '(no title)')
            click.echo(f'  [{i}] {title}')
            if desc:
                snippet = desc[:160] + ('...' if len(desc) > 160 else '')
                click.echo(f'      {snippet}')
            if url:
                click.echo(f'      {url}')
            click.echo()
