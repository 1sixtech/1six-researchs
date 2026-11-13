#!/usr/bin/env python3
"""Sample GossipStatus from many nodes at once, repeatedly.

Every round queries all target nodes concurrently on port 4002 and records each
node's `latest_height` with the local receive time. The highest height seen in
the round is taken as the network tip; a node's lag in a round is tip minus its
height, in blocks. Output: one JSON line per (round, node).

Usage: sample_status.py CRAWL_JSONL OUT_JSONL [--rounds N] [--interval S]
"""
import argparse
import asyncio
import json
import time

from crawl_gossip import parse_status, rpc


async def one(ip):
    try:
        ms, p = await rpc(ip, 4002, b"\x02")
        st = parse_status(p)
        return ip, time.time(), ms, (st[1] if st else None)
    except Exception:  # noqa: BLE001
        return ip, time.time(), None, None


async def main_async(ips, out_path, rounds, interval):
    out = open(out_path, "w")
    for k in range(rounds):
        t0 = time.time()
        res = await asyncio.gather(*(one(ip) for ip in ips))
        heights = [h for _, _, _, h in res if h is not None]
        tip = max(heights) if heights else None
        for ip, t, ms, h in res:
            out.write(json.dumps({"round": k, "ip": ip, "t": t, "connect_ms": ms,
                                  "height": h, "tip": tip}) + "\n")
        out.flush()
        await asyncio.sleep(max(0.0, interval - (time.time() - t0)))
    out.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("crawl")
    ap.add_argument("out")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args()
    ips = [r["ip"] for r in map(json.loads, open(a.crawl)) if r.get("status")]
    print(f"{len(ips)} synced nodes")
    asyncio.run(main_async(ips, a.out, a.rounds, a.interval))


if __name__ == "__main__":
    main()
