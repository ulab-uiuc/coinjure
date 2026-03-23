"""FOMC Dissent Count exhaustive arb — standalone execution script.

Places maker orders on all 5 exhaustive outcomes of KXFOMCDISSENTCOUNT-26MAR.
sum(YES) must = 1.0. When sum(bid) < 1.0, buy all YES at bid → guaranteed profit.

Usage:
    poetry run python data/kalshi-live/fomc_arb.py              # analyze only
    poetry run python data/kalshi-live/fomc_arb.py --execute 20  # place 20 contracts
    poetry run python data/kalshi-live/fomc_arb.py --status      # check fill status
    poetry run python data/kalshi-live/fomc_arb.py --cancel      # cancel all resting
    poetry run python data/kalshi-live/fomc_arb.py --watch       # auto-convert unfilled makers to taker
    poetry run python data/kalshi-live/fomc_arb.py --watch --fallback-minutes 30
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

os.chdir('/Users/ethanyang/prediction-market-cli')
from dotenv import load_dotenv
load_dotenv()

import httpx
from kalshi_python import Configuration
from kalshi_python.api.portfolio_api import PortfolioApi
from kalshi_python.api_client import ApiClient

EVENT = 'KXFOMCDISSENTCOUNT-26MAR'
STATE_FILE = Path('data/kalshi-live/fomc_arb_state.json')

# Legs where bid=0 must use taker (ask price). Threshold in dollars.
TAKER_THRESHOLD = 0.02  # legs with bid <= this use taker


def _client():
    config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')
    c = ApiClient(configuration=config)
    c.set_kalshi_auth(os.environ['KALSHI_API_KEY_ID'], os.environ['KALSHI_PRIVATE_KEY_PATH'])
    return c


async def fetch_markets(client):
    url = f'https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker={EVENT}&status=open&limit=20'
    auth = client.kalshi_auth.create_auth_headers('GET', url)
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(url, headers=auth)
        r.raise_for_status()
        return r.json().get('markets', [])


def analyze(markets):
    """Analyze arb opportunity. Returns (legs, edge_maker, edge_taker, total_maker, total_taker)."""
    legs = []
    for m in markets:
        ticker = m.get('ticker', '')
        yb = float(m.get('yes_bid_dollars', 0) or 0)
        ya = float(m.get('yes_ask_dollars', 0) or 0)
        legs.append({
            'ticker': ticker,
            'yes_bid': yb,
            'yes_ask': ya,
            'use_taker': yb <= TAKER_THRESHOLD,
        })

    # Sort by ticker for consistent ordering
    legs.sort(key=lambda x: x['ticker'])

    total_maker = sum(l['yes_bid'] for l in legs if not l['use_taker'])
    total_taker = sum(l['yes_ask'] for l in legs if l['use_taker'])
    # Taker fees: ~7% of price, minimum 1c per leg (empirical from Kalshi fills)
    taker_fees = 0.0
    for l in legs:
        if l['use_taker']:
            fee = max(0.01, round(l['yes_ask'] * 0.07 + 0.005, 2))  # ceil(7% × price)
            l['est_fee'] = fee
            taker_fees += fee
        else:
            l['est_fee'] = 0.0

    total_cost = total_maker + total_taker + taker_fees
    edge = 1.00 - total_cost
    profit_per_contract = edge

    return legs, profit_per_contract, total_cost


async def execute(client, legs, num_contracts):
    """Place orders for the arb."""
    url = 'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders'
    orders = []

    async with httpx.AsyncClient(timeout=15) as http:
        for leg in legs:
            if leg['use_taker']:
                price_cents = round(leg['yes_ask'] * 100)
                mode = 'taker'
            else:
                price_cents = round(leg['yes_bid'] * 100)
                mode = 'maker'

            price_cents = max(1, min(price_cents, 99))

            body = {
                'ticker': leg['ticker'],
                'action': 'buy',
                'side': 'yes',
                'type': 'limit',
                'count': num_contracts,
                'yes_price': price_cents,
                'client_order_id': str(uuid.uuid4()),
            }

            auth = client.kalshi_auth.create_auth_headers('POST', url)
            r = await http.post(url, json=body, headers={**auth, 'Content-Type': 'application/json'})

            if r.status_code == 201:
                order = r.json().get('order', r.json())
                order_id = order.get('order_id', '')
                status = order.get('status', '?')
                filled = order.get('fill_count_fp', '0')
                fee = order.get('taker_fees_dollars', '0')
                print(f'  ✅ {leg["ticker"]:40s} {mode:5s} @{price_cents:2d}c  x{num_contracts}  status={status}  filled={filled}  fee=${fee}')
                orders.append({
                    'order_id': order_id,
                    'ticker': leg['ticker'],
                    'price_cents': price_cents,
                    'mode': mode,
                    'count': num_contracts,
                    'status': status,
                    'filled': filled,
                })
            else:
                print(f'  ❌ {leg["ticker"]:40s} {mode:5s} @{price_cents:2d}c  HTTP {r.status_code}: {r.text[:80]}')
                orders.append({
                    'order_id': '',
                    'ticker': leg['ticker'],
                    'error': r.text[:200],
                })

            await asyncio.sleep(0.3)

    return orders


async def check_status(client):
    """Check fill status of existing orders."""
    if not STATE_FILE.exists():
        print('No state file found. Run --execute first.')
        return

    state = json.loads(STATE_FILE.read_text())
    orders = state.get('orders', [])
    total_filled_cost = 0
    total_filled_legs = 0
    all_filled = True

    async with httpx.AsyncClient(timeout=15) as http:
        for o in orders:
            oid = o.get('order_id', '')
            if not oid:
                print(f'  ❌ {o["ticker"]:40s} no order_id (failed)')
                all_filled = False
                continue

            url = f'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/{oid}'
            auth = client.kalshi_auth.create_auth_headers('GET', url)
            r = await http.get(url, headers=auth)
            if r.status_code != 200:
                print(f'  ❌ {o["ticker"]:40s} HTTP {r.status_code}')
                all_filled = False
                continue

            detail = r.json().get('order', r.json())
            status = detail.get('status', '?')
            filled = float(detail.get('fill_count_fp', 0) or 0)
            initial = float(detail.get('initial_count_fp', 0) or 0)
            remaining = float(detail.get('remaining_count_fp', 0) or 0)
            taker_cost = float(detail.get('taker_fill_cost_dollars', 0) or 0)
            maker_cost = float(detail.get('maker_fill_cost_dollars', 0) or 0)
            taker_fee = float(detail.get('taker_fees_dollars', 0) or 0)
            maker_fee = float(detail.get('maker_fees_dollars', 0) or 0)

            fill_cost = taker_cost + maker_cost
            total_fees = taker_fee + maker_fee
            total_filled_cost += fill_cost + total_fees

            is_done = status in ('executed', 'filled') or remaining == 0
            if not is_done:
                all_filled = False

            icon = '✅' if is_done else '⏳'
            print(f'  {icon} {o["ticker"]:40s} {status:10s} filled={filled:.0f}/{initial:.0f}  cost=${fill_cost:.2f}  fee=${total_fees:.2f}')

            if filled > 0:
                total_filled_legs += 1

    count = state.get('num_contracts', 0)
    payout = count * 1.00
    print()
    print(f'Filled legs: {total_filled_legs}/{len(orders)}')
    print(f'Total cost so far: ${total_filled_cost:.2f}')
    print(f'Expected payout at settlement: ${payout:.2f}')
    print(f'Expected profit: ${payout - total_filled_cost:.2f}')
    if all_filled:
        print(f'🎯 ALL LEGS FILLED — guaranteed profit at settlement!')
    else:
        print(f'⏳ Waiting for maker fills...')


async def watch_and_fallback(client, fallback_minutes=30):
    """Watch unfilled maker orders and auto-convert to taker after timeout."""
    if not STATE_FILE.exists():
        print('No state file found. Run --execute first.')
        return

    state = json.loads(STATE_FILE.read_text())
    orders = state.get('orders', [])
    executed_at = datetime.fromisoformat(state.get('executed_at', ''))
    fallback_seconds = fallback_minutes * 60

    print(f'Watching {len(orders)} orders, fallback after {fallback_minutes}min...')

    while True:
        elapsed = (datetime.now() - executed_at).total_seconds()
        all_filled = True
        unfilled_makers = []

        async with httpx.AsyncClient(timeout=15) as http:
            for o in orders:
                oid = o.get('order_id', '')
                if not oid or o.get('mode') != 'maker':
                    continue

                url = f'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/{oid}'
                auth = client.kalshi_auth.create_auth_headers('GET', url)
                try:
                    r = await http.get(url, headers=auth)
                    if r.status_code != 200:
                        all_filled = False
                        print(f'  Warning: cannot check {o["ticker"]} (HTTP {r.status_code})')
                        continue
                    detail = r.json().get('order', r.json())
                    status = detail.get('status', '?')
                    remaining = float(detail.get('remaining_count_fp', 0) or 0)

                    if status in ('executed', 'filled') or remaining == 0:
                        continue  # fully filled
                    all_filled = False
                    unfilled_makers.append({
                        'order_id': oid,
                        'ticker': o['ticker'],
                        'remaining': int(remaining),
                        'status': status,
                    })
                except Exception as e:
                    all_filled = False
                    print(f'  Error checking {o["ticker"]}: {e}')

        if all_filled:
            print(f'[{datetime.now():%H:%M:%S}] All maker orders filled!')
            break

        if elapsed >= fallback_seconds:
            if unfilled_makers:
                print(f'\n[{datetime.now():%H:%M:%S}] Fallback triggered after {elapsed/60:.0f}min')
                await _convert_makers_to_taker(client, unfilled_makers)
            else:
                print(f'[{datetime.now():%H:%M:%S}] Cannot verify orders (API errors), timeout reached')
            break

        # Status update
        if unfilled_makers:
            tickers = ', '.join(u['ticker'].split('-')[-1] for u in unfilled_makers)
            print(
                f'[{datetime.now():%H:%M:%S}] {len(unfilled_makers)} unfilled makers '
                f'({tickers}), {elapsed/60:.0f}/{fallback_minutes}min elapsed'
            )
        else:
            print(f'[{datetime.now():%H:%M:%S}] Cannot verify some orders, retrying in 60s...')
        await asyncio.sleep(60)


async def _convert_makers_to_taker(client, unfilled_makers):
    """Cancel unfilled maker orders and re-place as taker at ask."""
    # First fetch current market prices
    markets = await fetch_markets(client)
    price_map = {}
    for m in markets:
        ticker = m.get('ticker', '')
        price_map[ticker] = float(m.get('yes_ask_dollars', 0) or 0)

    async with httpx.AsyncClient(timeout=15) as http:
        for um in unfilled_makers:
            oid = um['order_id']
            ticker = um['ticker']
            remaining = um['remaining']

            # Cancel the maker order
            cancel_url = f'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/{oid}'
            auth = client.kalshi_auth.create_auth_headers('DELETE', cancel_url)
            r = await http.delete(cancel_url, headers=auth)
            if r.status_code not in (200, 204):
                print(f'  Failed to cancel {ticker}: HTTP {r.status_code}')
                continue
            print(f'  Cancelled maker: {ticker} ({remaining} remaining)')

            # Re-place as taker at ask
            ask = price_map.get(ticker, 0)
            if ask <= 0:
                print(f'  No ask price for {ticker}, skipping re-place')
                continue

            price_cents = min(round(ask * 100), 99)  # Kalshi prices: 1-99c
            if price_cents < 1:
                price_cents = 1
            body = {
                'ticker': ticker,
                'action': 'buy',
                'side': 'yes',
                'type': 'limit',
                'count': remaining,
                'yes_price': price_cents,
                'client_order_id': str(uuid.uuid4()),
            }
            order_url = 'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders'
            auth2 = client.kalshi_auth.create_auth_headers('POST', order_url)
            r2 = await http.post(
                order_url, json=body,
                headers={**auth2, 'Content-Type': 'application/json'},
            )
            if r2.status_code == 201:
                order = r2.json().get('order', r2.json())
                fee = order.get('taker_fees_dollars', '0')
                print(f'  Taker re-placed: {ticker} @{price_cents}c x{remaining} fee=${fee}')
            else:
                print(f'  Taker re-place failed: {ticker} HTTP {r2.status_code}: {r2.text[:80]}')

            await asyncio.sleep(0.3)


async def cancel_resting(client):
    """Cancel all resting orders."""
    url = 'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders?status=resting&limit=50'
    auth = client.kalshi_auth.create_auth_headers('GET', url)
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(url, headers=auth)
        orders = r.json().get('orders', [])
        fomc_orders = [o for o in orders if EVENT in o.get('ticker', '')]
        print(f'Resting FOMC orders: {len(fomc_orders)}')
        for o in fomc_orders:
            oid = o.get('order_id')
            ticker = o.get('ticker', '?')
            cancel_url = f'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/{oid}'
            auth2 = client.kalshi_auth.create_auth_headers('DELETE', cancel_url)
            r2 = await http.delete(cancel_url, headers=auth2)
            if r2.status_code in (200, 204):
                print(f'  Cancelled: {ticker}')
            else:
                print(f'  Failed: {ticker} HTTP {r2.status_code}')


async def main():
    client = _client()

    if '--cancel' in sys.argv:
        await cancel_resting(client)
        return

    if '--status' in sys.argv:
        await check_status(client)
        return

    if '--watch' in sys.argv:
        fb_min = 30
        if '--fallback-minutes' in sys.argv:
            idx = sys.argv.index('--fallback-minutes')
            fb_min = int(sys.argv[idx + 1])
        await watch_and_fallback(client, fallback_minutes=fb_min)
        return

    # Fetch and analyze
    markets = await fetch_markets(client)
    legs, profit_per_contract, total_cost = analyze(markets)

    bal = PortfolioApi(client).get_balance()
    balance = int(bal.balance) / 100

    print(f'=== FOMC Dissent Count Arb Analysis | {datetime.now():%Y-%m-%d %H:%M} ===')
    print(f'Balance: ${balance:.2f}')
    print()
    print(f'{"Market":42s} {"bid":>5s} {"ask":>5s} {"mode":>6s} {"cost":>6s}')
    print('-' * 68)
    for leg in legs:
        mode = 'TAKER' if leg['use_taker'] else 'MAKER'
        cost = leg['yes_ask'] if leg['use_taker'] else leg['yes_bid']
        fee = leg.get('est_fee', 0)
        print(f'{leg["ticker"]:42s} {leg["yes_bid"]:5.2f} {leg["yes_ask"]:5.2f} {mode:>6s} {cost+fee:5.2f}')

    print('-' * 68)
    print(f'{"Total cost per contract":42s} {"":>5s} {"":>5s} {"":>6s} {total_cost:5.2f}')
    print(f'{"Payout at settlement":42s} {"":>5s} {"":>5s} {"":>6s} {"1.00":>5s}')
    print(f'{"Profit per contract":42s} {"":>5s} {"":>5s} {"":>6s} {profit_per_contract:5.2f}')
    print()

    if not legs or profit_per_contract <= 0:
        print('❌ No profitable arb opportunity right now.')
        return

    # Execute mode
    if '--execute' in sys.argv:
        idx = sys.argv.index('--execute')
        num = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 10

        cost_total = total_cost * num
        if cost_total > balance * 0.8:
            print(f'⚠️  Total cost ${cost_total:.2f} > 80% of balance ${balance:.2f}. Reduce size.')
            return

        print(f'Executing: {num} contracts × ${total_cost:.2f} = ${cost_total:.2f}')
        print(f'Expected profit: {num} × ${profit_per_contract:.2f} = ${num * profit_per_contract:.2f}')
        print()

        orders = await execute(client, legs, num)

        # Save state
        state = {
            'executed_at': datetime.now().isoformat(),
            'event': EVENT,
            'num_contracts': num,
            'total_cost_estimate': cost_total,
            'profit_estimate': num * profit_per_contract,
            'orders': orders,
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))
        print(f'\nState saved to {STATE_FILE}')
        print(f'Run with --status to check fill progress.')
    else:
        max_contracts = int(balance * 0.6 / total_cost) if total_cost > 0 else 0
        print(f'Recommended size: {max_contracts} contracts (60% of balance)')
        print(f'Expected profit: ${max_contracts * profit_per_contract:.2f}')
        print(f'\nRun: poetry run python data/kalshi-live/fomc_arb.py --execute {max_contracts}')


if __name__ == '__main__':
    asyncio.run(main())
