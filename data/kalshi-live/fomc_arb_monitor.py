"""FOMC arb fill monitor — checks status and sends Telegram alert on changes."""
import asyncio
import json
import os
import urllib.request
from pathlib import Path

os.chdir('/Users/ethanyang/prediction-market-cli')
from dotenv import load_dotenv
load_dotenv()

import httpx
from kalshi_python import Configuration
from kalshi_python.api_client import ApiClient

STATE_FILE = Path('data/kalshi-live/fomc_arb_state.json')
MONITOR_STATE = Path('data/kalshi-live/fomc_arb_monitor_state.json')

BOT_TOKEN = '8083707922:AAHfcwoUg9Yg2vkvT-n0n723ebk1BEu95Go'
CHAT_ID = '7829844554'


def send_tg(message: str):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = json.dumps({'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get('ok', False)
    except Exception as e:
        print(f'Telegram send failed: {e}')
        return False


def _client():
    config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')
    c = ApiClient(configuration=config)
    c.set_kalshi_auth(os.environ['KALSHI_API_KEY_ID'], os.environ['KALSHI_PRIVATE_KEY_PATH'])
    return c


async def monitor():
    if not STATE_FILE.exists():
        print('No state file.')
        return

    state = json.loads(STATE_FILE.read_text())
    orders = state.get('orders', [])
    client = _client()

    # Load previous monitor state
    prev_fills = {}
    if MONITOR_STATE.exists():
        prev_fills = json.loads(MONITOR_STATE.read_text())

    current_fills = {}
    changes = []
    total_cost = 0
    total_filled_legs = 0
    all_filled = True

    async with httpx.AsyncClient(timeout=15) as http:
        for o in orders:
            oid = o.get('order_id', '')
            ticker = o.get('ticker', '')
            if not oid:
                continue

            url = f'https://api.elections.kalshi.com/trade-api/v2/portfolio/orders/{oid}'
            auth = client.kalshi_auth.create_auth_headers('GET', url)
            r = await http.get(url, headers=auth)
            if r.status_code != 200:
                continue

            detail = r.json().get('order', r.json())
            filled = float(detail.get('fill_count_fp', 0) or 0)
            initial = float(detail.get('initial_count_fp', 0) or 0)
            status = detail.get('status', '?')
            taker_cost = float(detail.get('taker_fill_cost_dollars', 0) or 0)
            maker_cost = float(detail.get('maker_fill_cost_dollars', 0) or 0)
            taker_fee = float(detail.get('taker_fees_dollars', 0) or 0)
            maker_fee = float(detail.get('maker_fees_dollars', 0) or 0)
            price = detail.get('yes_price_dollars', '?')

            total_cost += taker_cost + maker_cost + taker_fee + maker_fee
            current_fills[ticker] = filled

            if filled > 0:
                total_filled_legs += 1

            is_done = status in ('executed', 'filled') or filled >= initial
            if not is_done:
                all_filled = False

            # Check for changes
            prev = prev_fills.get(ticker, 0)
            if filled > prev:
                changes.append(f'• {ticker}: {prev:.0f} → {filled:.0f}/{initial:.0f} @{price}')

    # Save current state
    MONITOR_STATE.write_text(json.dumps(current_fills))

    num = state.get('num_contracts', 20)
    payout = num * 1.00

    if changes:
        icon = '🎯' if all_filled else '📈'
        msg = f'{icon} <b>FOMC套利成交更新</b>\n\n'
        msg += '\n'.join(changes)
        msg += f'\n\n已成交legs: {total_filled_legs}/{len(orders)}'
        msg += f'\n总成本: ${total_cost:.2f}'
        msg += f'\n预期收益: ${payout:.2f}'
        msg += f'\n预期利润: ${payout - total_cost:.2f}'
        if all_filled:
            msg += '\n\n🎯 所有legs已成交！套利锁定成功！'
        send_tg(msg)
        print(f'Changes detected, notification sent: {changes}')
    else:
        print(f'No changes. Filled legs: {total_filled_legs}/{len(orders)}, cost=${total_cost:.2f}')

    if all_filled:
        msg = f'🎯 <b>FOMC套利全部成交！</b>\n\n总成本: ${total_cost:.2f}\n结算收益: ${payout:.2f}\n确定利润: ${payout - total_cost:.2f}\n\n到期自动结算，无需操作。'
        send_tg(msg)


if __name__ == '__main__':
    asyncio.run(monitor())
