#!/usr/bin/env python
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swm_agent.data.live.live_data_source import LiveDataSource


async def run(args):
    os.makedirs(args.data_dir, exist_ok=True)

    data_source = LiveDataSource(
        event_cache_file=os.path.join(args.data_dir, 'events_cache.jsonl'),
        polling_interval=args.interval,
    )

    print(f'Starting LiveDataSource (polling every {args.interval} seconds)')
    await data_source.start()

    events_processed = 0
    print('Waiting for events...')

    while True:
        try:
            event = await data_source.get_next_event()

            if event:
                events_processed += 1
                event_id = event.get('id', 'unknown')
                print(f'Received event {events_processed}: {event_id}')

                if args.save:
                    event_file = os.path.join(
                        args.data_dir,
                        f"event_{event_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    )
                    with open(event_file, 'w') as f:
                        json.dump(event, f, indent=2)
            else:
                await asyncio.sleep(1.0)

        except KeyboardInterrupt:
            print('\nShutting down')
            break
        except Exception as e:
            print(f'Error: {e}')
            await asyncio.sleep(1.0)

    print(f'Processed {events_processed} events.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Run LiveDataSource to collect PolyMarket events'
    )
    parser.add_argument(
        '--interval', type=float, default=30.0, help='Polling interval in seconds'
    )
    parser.add_argument(
        '--data-dir', type=str, default='../data', help='Directory to store event data'
    )
    parser.add_argument(
        '--save', action='store_true', help='Save events to individual JSON files'
    )
    parser.add_argument('--quiet', action='store_true', help='Reduce output verbosity')

    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('\nScript interrupted')
