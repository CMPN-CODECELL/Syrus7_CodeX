"""Connectivity check - run this FIRST:   python check_021.py

Logs in to the 021 sandbox with the credentials in .env, finds your instruments, reads your positions and listens to the
live price feed for ~10 seconds. It places NO orders.
"""
import asyncio
import logging
import sys

from tradeshield.broker.o21 import O21Broker
from tradeshield.config import Settings


async def main():
    s = Settings()
    if not (s.o21_username and s.o21_password):
        sys.exit("Put O21_USERNAME and O21_PASSWORD into the .env file first (copy .env.example to .env).")
    b = O21Broker(s)
    print(f"1/4 logging in as {s.o21_username} ...")
    try:
        await b.start()
    except RuntimeError as e:
        sys.exit(f"FAILED: {e}")
    print("    login OK")
    print(f"2/4 instrument tokens: {b.sym2tok}")
    print(f"3/4 account positions: {await b.get_positions() or 'none (flat)'}")
    print(f"    open orders: {len(await b.get_open_orders())}")
    print("4/4 listening to live prices for 10 seconds (if the market is closed you only get the last snapshot) ...")
    seen = {}
    b.subscribe_ticks(lambda t: seen.__setitem__(t.symbol, (t.price, t.open)))
    await asyncio.sleep(10)
    if seen:
        for sym, (px, op) in seen.items():
            print(f"    {sym}: last price {px}   open {op}")
    else:
        print("    no ticks received. Market closed or websocket blocked? (the platform still works for orders)")
    await b.stop()
    print("Done.")


logging.basicConfig(level=logging.WARNING)
asyncio.run(main())
