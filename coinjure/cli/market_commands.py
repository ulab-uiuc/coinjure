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
        click.echo(f'\n[polymarket] Event slug: {slug}')
        for m in markets:
            click.echo(
                f'  ID: {m["id"]}  Bid: {m.get("best_bid","")}  Ask: {m.get("best_ask","")}'
            )
            click.echo(f'    {m.get("question","")[:80]}')
        click.echo()
        return

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
    click.echo(f'\n[{exchange}] {question}')
    click.echo(f'  ID: {market_id}')
    for key in (
        'best_bid',
        'best_ask',
        'yes_bid',
        'yes_ask',
        'volume',
        'end_date',
        'token_ids',
        'event_id',
    ):
        val = info.get(key)
        if val not in (None, '', []):
            click.echo(f'  {key}: {val}')
    desc = info.get('description', '')
    if desc:
        click.echo(f'  description: {desc[:200]}{"..." if len(desc) > 200 else ""}')
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
@click.option('--market-id-a', required=True, help='Market ID for leg A.')
@click.option('--market-id-b', required=True, help='Market ID for leg B.')
@click.option(
    '--exchange',
    'exc',
    type=click.Choice(['polymarket', 'kalshi', 'auto']),
    default='auto',
    show_default=True,
    help='Exchange for both markets. Use "auto" to detect per market ID (numeric=polymarket, else=kalshi).',
)
@click.option(
    '--exchange-a',
    'exc_a',
    type=click.Choice(['polymarket', 'kalshi']),
    default=None,
    help='Exchange for market A (overrides --exchange).',
)
@click.option(
    '--exchange-b',
    'exc_b',
    type=click.Choice(['polymarket', 'kalshi']),
    default=None,
    help='Exchange for market B (overrides --exchange).',
)
@click.option(
    '--spread-type',
    default='correlated',
    show_default=True,
    help='Relation type (same_event, complementary, implication, exclusivity, correlated).',
)
@click.option('--hypothesis', default='', help='Price relationship hypothesis.')
@click.option('--reasoning', default='', help='Why these markets are related.')
@click.option('--json', 'as_json', is_flag=True, default=False, help='Emit JSON')
def relations_add(
    market_id_a: str,
    market_id_b: str,
    exc: str,
    exc_a: str | None,
    exc_b: str | None,
    spread_type: str,
    hypothesis: str,
    reasoning: str,
    as_json: bool,
) -> None:
    """Create a relation between two markets.

    Fetches market info from the API and persists the relation.

    \b
      coinjure market relations add \\
        --market-id-a 610380 --market-id-b 610381 \\
        --spread-type correlated \\
        --hypothesis "p_A ~ p_B (positive)" \\
        --reasoning "election called is prerequisite for held" --json
    """
    from coinjure.market.relations import MarketRelation, RelationStore

    # Resolve per-market exchange
    resolved_exc_a = exc_a or (exc if exc != 'auto' else _detect_exchange(market_id_a))
    resolved_exc_b = exc_b or (exc if exc != 'auto' else _detect_exchange(market_id_b))

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

    try:
        info_a = asyncio.run(_fetch_info(market_id_a, resolved_exc_a))
        info_b = asyncio.run(_fetch_info(market_id_b, resolved_exc_b))
    except Exception as exc_err:
        raise click.ClickException(
            f'Failed to fetch market info: {exc_err}'
        ) from exc_err

    rid = f'{market_id_a[:8]}-{market_id_b[:8]}'

    rel = MarketRelation(
        relation_id=rid,
        market_a=info_a,
        market_b=info_b,
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

    click.echo(f'\nRelation created: {rid}')
    click.echo(f'  A: [{info_a.get("platform")}] {info_a.get("question", "")[:60]}')
    click.echo(f'  B: [{info_b.get("platform")}] {info_b.get("question", "")[:60]}')
    click.echo(f'  type={spread_type} hypothesis={hypothesis}')
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

    click.echo(f'\n{len(relations)} market relation(s):\n')
    for r in relations:
        ma = r.market_a
        mb = r.market_b
        click.echo(
            f'  [{r.relation_id}] {r.spread_type}  conf={r.confidence:.2f}  '
            f'status={r.status}'
        )
        click.echo(f'    A: [{ma.get("platform", "?")}] {ma.get("question", "?")[:60]}')
        click.echo(f'    B: [{mb.get("platform", "?")}] {mb.get("question", "?")[:60]}')
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
# market discover (multi-keyword search + structural pair detection)
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
    '--auto-pair/--no-auto-pair',
    'auto_pair',
    default=True,
    show_default=True,
    help='Auto-detect and persist intra-event structural relations '
    '(implication, exclusivity, complementary). Use --no-auto-pair to disable.',
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
    auto_pair: bool,
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
        # Over-fetch to compensate for zombie filtering downstream
        fetch_limit = limit * 2

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

        has_filters = query or tag

        if query or tag:
            for q in query:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(
                        await polymarket_search_markets(
                            q,
                            fetch_limit,
                            with_rules=with_rules,
                        )
                    )
                if exchange in ('kalshi', 'both'):
                    _merge_kalshi(
                        await kalshi_search_markets(
                            q,
                            fetch_limit,
                            kalshi_api_key_id,
                            kalshi_private_key_path,
                            with_rules=with_rules,
                        )
                    )
            for t in tag:
                if exchange in ('polymarket', 'both'):
                    _merge_poly(
                        await polymarket_list_markets(
                            fetch_limit,
                            tag=t,
                            with_rules=with_rules,
                        )
                    )
        else:
            if exchange in ('polymarket', 'both'):
                poly_markets = await polymarket_list_markets(
                    fetch_limit,
                    with_rules=with_rules,
                )
            if exchange in ('kalshi', 'both'):
                kalshi_markets = await kalshi_list_markets(
                    fetch_limit,
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

    poly_markets = [m for m in poly_markets if _is_alive(m)][:limit]
    kalshi_markets = [m for m in kalshi_markets if _is_alive(m)][:limit]

    # ── Annotate with existing relations ──────────────────────────────

    from coinjure.market.relations import RelationStore

    store = RelationStore()
    all_relations = store.list()

    # Build a set of market IDs already in relations
    related_ids: dict[str, list[str]] = {}  # market_id → [relation_id, ...]
    for r in all_relations:
        for mkt in (r.market_a, r.market_b):
            for key in ('id', 'market_id', 'ticker'):
                mid = mkt.get(key, '')
                if mid:
                    related_ids.setdefault(mid, []).append(r.relation_id)

    # Tag each market with its existing relations
    for mk in poly_markets:
        mid = mk.get('id', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]
    for mk in kalshi_markets:
        mid = mk.get('ticker', '')
        if mid and mid in related_ids:
            mk['existing_relations'] = related_ids[mid]

    # Compact summary of existing relations for agent context
    relation_summary = [
        {
            'relation_id': r.relation_id,
            'type': r.spread_type,
            'status': r.status,
            'market_a_id': r.market_a.get('id', r.market_a.get('ticker', '')),
            'market_b_id': r.market_b.get('id', r.market_b.get('ticker', '')),
            'market_a_question': r.market_a.get('question', '')[:60],
            'market_b_question': r.market_b.get('question', '')[:60],
        }
        for r in all_relations
    ]

    # ── Auto-pair ───────────────────────────────────────────────────────

    auto_pair_summary: dict[str, Any] | None = None
    if auto_pair:
        from coinjure.market.auto_pair import auto_pair_markets

        result = auto_pair_markets(poly_markets, kalshi_markets)

        # Persist candidates to relation store
        stored_new = 0
        if result.candidates:
            from coinjure.market.relations import RelationStore

            store = RelationStore()
            stored_new = store.add_batch(result.candidates)

        auto_pair_summary = {
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
                    'market_a_id': r.market_a.get('id', ''),
                    'market_b_id': r.market_b.get('id', ''),
                    'market_a': r.market_a.get('question', '')[:60],
                    'market_b': r.market_b.get('question', '')[:60],
                    'current_mid_a': r.market_a.get('current_mid'),
                    'current_mid_b': r.market_b.get('current_mid'),
                    'current_arb': r.market_a.get('current_arb', 0),
                }
                for r in result.candidates
            ],
        }

    # ── Output ─────────────────────────────────────────────────────────

    total = len(poly_markets) + len(kalshi_markets)

    if as_json:
        payload: dict[str, Any] = {
            'ok': True,
            'markets': {
                'polymarket': poly_markets,
                'kalshi': kalshi_markets,
            },
            'total_markets': total,
            'existing_relations': relation_summary,
            'existing_relation_count': len(relation_summary),
        }
        if auto_pair_summary is not None:
            payload['auto_pair'] = auto_pair_summary
        click.echo(json.dumps(payload, default=str))
        return

    parts = [f'"{q}"' for q in query] + [f'tag:{t}' for t in tag]
    query_desc = ', '.join(parts) if parts else 'top by volume'
    click.echo(
        f'\nDiscovered {len(poly_markets)} Polymarket + {len(kalshi_markets)} Kalshi '
        f'markets (queries: {query_desc})\n'
    )

    def _fmt_table(
        markets: list[dict],
        platform: str,
    ) -> None:
        if not markets:
            return
        click.echo(f'  [{platform}] {len(markets)} markets')
        click.echo(f'  {"ID":>12}  {"Bid":>6}  {"Ask":>6}  {"Volume":>12}  Question')
        click.echo(f'  {"─"*12}  {"─"*6}  {"─"*6}  {"─"*12}  {"─"*50}')
        for m in markets:
            mid = m.get('id') or m.get('ticker', '')
            bid = m.get('best_bid') or m.get('yes_bid', '')
            ask = m.get('best_ask') or m.get('yes_ask', '')
            vol = m.get('volume', 0)
            q = m.get('question') or m.get('title', '')
            try:
                bid_s = f'{float(bid):6.3f}' if bid not in (None, '') else '     —'
            except (ValueError, TypeError):
                bid_s = '     —'
            try:
                ask_s = f'{float(ask):6.3f}' if ask not in (None, '') else '     —'
            except (ValueError, TypeError):
                ask_s = '     —'
            try:
                vol_s = f'{float(str(vol).replace(",", "")):12.0f}'
            except (ValueError, TypeError):
                vol_s = '           0'
            click.echo(f'  {mid:>12}  {bid_s}  {ask_s}  {vol_s}  {q[:55]}')
            if with_rules:
                desc = m.get('description') or m.get('rules_primary', '')
                if desc:
                    # Show first 120 chars of rules
                    click.echo(f'  {"":>12}  Rules: {desc[:120]}...')
        click.echo()

    _fmt_table(poly_markets, 'Polymarket')
    _fmt_table(kalshi_markets, 'Kalshi')

    if auto_pair_summary:
        s = auto_pair_summary
        td = s['total_detected']
        wc = s['candidate_count']
        sn = s.get('stored_new', 0)
        click.echo(
            f'  Auto-pair: {td} structural pairs detected, {wc} with current opportunity'
        )
        if wc > 0:
            click.echo(f'    Stored {sn} new relation(s) ({wc - sn} already in store)')
        if s['by_type']:
            click.echo(f'    By type: {s["by_type"]}')
        click.echo()
        for r in s['candidates'][:20]:
            arb = r.get('current_arb', 0)
            arb_s = f'   arb={arb:.3f}' if arb else ''
            click.echo(
                f'    [{r["type"]}]  {r["market_a_id"]} <-> {r["market_b_id"]}{arb_s}'
            )
            mid_a = r.get('current_mid_a')
            mid_b = r.get('current_mid_b')
            mid_a_s = f'  mid={mid_a:.3f}' if mid_a is not None else ''
            mid_b_s = f'  mid={mid_b:.3f}' if mid_b is not None else ''
            click.echo(f'      A: {r["market_a"]}{mid_a_s}')
            click.echo(f'      B: {r["market_b"]}{mid_b_s}')
            if r['type'] == 'implication' and mid_a is not None and mid_b is not None:
                click.echo(
                    f'      Violation: A ({mid_a:.3f}) > B ({mid_b:.3f}), arb = {arb:.3f}'
                )
            elif (
                r['type'] in ('exclusivity', 'complementary')
                and mid_a is not None
                and mid_b is not None
            ):
                click.echo(
                    f'      Violation: A ({mid_a:.3f}) + B ({mid_b:.3f})'
                    f' = {mid_a + mid_b:.3f} > 1, arb = {arb:.3f}'
                )
            else:
                click.echo(f'      {r["reasoning"]}')
            click.echo()
        if len(s['candidates']) > 20:
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
        format_article,
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

    click.echo(f'Fetched {len(articles)} article(s) from {source}:\n')
    for i, article in enumerate(articles, 1):
        click.echo(f'[{i}]')
        click.echo(format_article(article))
        click.echo()
