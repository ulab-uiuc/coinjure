"""Kalshi live trading status report."""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

os.chdir('/Users/ethanyang/prediction-market-cli')
from dotenv import load_dotenv
load_dotenv()

def report():
    import subprocess
    from kalshi_python import Configuration
    from kalshi_python.api.portfolio_api import PortfolioApi
    from kalshi_python.api_client import ApiClient

    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Running processes
    ps = subprocess.run(
        ['bash', '-c', 'ps aux | grep kalshi_live | grep -v grep | wc -l'],
        capture_output=True, text=True,
    )
    running = ps.stdout.strip()

    # Kalshi balance and positions
    config = Configuration(host='https://api.elections.kalshi.com/trade-api/v2')
    client = ApiClient(configuration=config)
    client.set_kalshi_auth(os.environ['KALSHI_API_KEY_ID'], os.environ['KALSHI_PRIVATE_KEY_PATH'])
    portfolio = PortfolioApi(client)

    bal = portfolio.get_balance()
    balance = int(bal.balance) / 100

    positions = portfolio.get_positions()
    pos_list = positions.market_positions if hasattr(positions, 'market_positions') else []

    # Engine status via sockets
    from coinjure.engine.control import run_command
    sockets = {
        'KXFEDDECISION-26MAR': 23316,
        'KXFEDDECISION-26APR': 23322,
        'KXFOMCDISSENTCOUNT': 23336,
        'KXTRUMPOUT27': 23339,
        'KXINSURRECTION': 23352,
    }
    total_decisions = 0
    total_executed = 0
    strategy_status = []
    for label, pid in sockets.items():
        sock = Path(f'/Users/ethanyang/.coinjure/engine-{pid}.sock')
        try:
            status = run_command('status', socket_path=sock)
            d = status.get('decisions', 0)
            e = status.get('executed', 0)
            total_decisions += d
            total_executed += e
            if d > 0 or e > 0:
                strategy_status.append(f'  {label}: decisions={d} executed={e}')
        except Exception:
            strategy_status.append(f'  {label}: offline')

    # Recent fills
    import httpx
    async def get_recent_fills():
        url = 'https://api.elections.kalshi.com/trade-api/v2/portfolio/fills?limit=5'
        auth = client.kalshi_auth.create_auth_headers('GET', url)
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(url, headers=auth)
            return r.json().get('fills', [])
    fills = asyncio.run(get_recent_fills())

    print(f'=== Kalshi Live Trading Report | {now} ===')
    print(f'Running: {running}/5 processes | Balance: ${balance:.2f} | Positions: {len(pos_list)}')
    print(f'Decisions: {total_decisions} | Executed: {total_executed}')
    print()

    if strategy_status:
        print('Strategy Status:')
        for s in strategy_status:
            print(s)
        print()

    if pos_list:
        print('Open Positions:')
        for p in pos_list:
            cost = abs(p.total_cost or 0) / 100
            print(f'  {p.ticker}: qty={p.position} cost=${cost:.2f}')
        print()

    if fills:
        print('Recent Fills:')
        for f in fills[:5]:
            t = f.get('created_time', '?')[:19]
            print(f'  {t} {f["ticker"]} {f["action"]} {f["side"]} @ {f.get("yes_price_dollars","?")}')
        print()

    print('=' * 50)

if __name__ == '__main__':
    report()
