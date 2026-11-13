#!/usr/bin/env python3
"""Crawl Hyperliquid's gossip network from a seed list.

Wire format (learned from a packet capture of the official node, 2026-11-13):
every frame is a 4-byte big-endian payload length, one flag byte (0), then the
payload. On port 4002, request 0x01 is `Peers` and request 0x02 is
`GossipStatus`. On port 4001 a node opens a block stream with the greeting
0x01 0x00 0x00; a node with no free slot answers 0x04.

For every node reached, the crawler records: TCP connect time from the vantage
point, its peer list (`Peers`), its status counters (`GossipStatus`) and the
first reply byte to a stream greeting on 4001 (the connection is closed right
after, so no block data is pulled). Output: one JSON object per node.

Usage: crawl_gossip.py SEEDS_JSON OUT_JSONL [--concurrency N] [--no-greeting]
"""
import argparse
import asyncio
import json
import struct
import time

TIMEOUT = 3.0


def frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + b"\x00" + payload


async def read_frame(reader):
    hdr = await reader.readexactly(5)
    (n,) = struct.unpack(">I", hdr[:4])
    return await reader.readexactly(n)


def varint(b: bytes, i: int):
    """bincode varint: <251 literal; 251 u16; 252 u32; 253 u64 (little endian)."""
    t = b[i]
    if t < 251:
        return t, i + 1
    size = {251: 2, 252: 4, 253: 8}[t]
    return int.from_bytes(b[i + 1:i + 1 + size], "little"), i + 1 + size


def parse_peers(p: bytes):
    assert p[0] == 1, p[:4].hex()
    n, i = varint(p, 1)
    out = []
    for _ in range(n):
        if p[i] == 0:  # IPv4
            ip = ".".join(str(x) for x in p[i + 1:i + 5])
            i += 5
        else:  # IPv6, not seen so far
            ip = p[i + 1:i + 17].hex()
            i += 17
        flags = list(p[i:i + 2])
        i += 2
        out.append({"ip": ip, "flags": flags})
    return out


def parse_status(p: bytes):
    assert p[0] == 2, p[:4].hex()
    if len(p) < 2 or p[1] == 0:
        return None
    vals, i = [], 2
    while i < len(p):
        v, i = varint(p, i)
        vals.append(v)
    return vals


async def rpc(ip, port, req):
    t0 = time.perf_counter()
    r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), TIMEOUT)
    connect_ms = (time.perf_counter() - t0) * 1000
    try:
        w.write(frame(req))
        await w.drain()
        payload = await asyncio.wait_for(read_frame(r), TIMEOUT)
        return connect_ms, payload
    finally:
        w.close()


async def probe(ip, greeting=True):
    rec = {"ip": ip, "t": time.time()}
    try:
        ms, p = await rpc(ip, 4002, b"\x01")
        rec["connect_ms"] = round(ms, 2)
        rec["peers"] = parse_peers(p)
    except Exception as e:  # noqa: BLE001
        rec["peers_err"] = type(e).__name__
    try:
        ms, p = await rpc(ip, 4002, b"\x02")
        rec.setdefault("connect_ms", round(ms, 2))
        rec["status_t"] = time.time()
        rec["status"] = parse_status(p)
    except Exception as e:  # noqa: BLE001
        rec["status_err"] = type(e).__name__
    if greeting:
        try:
            _, p = await rpc(ip, 4001, b"\x01\x00\x00")
            rec["greeting_reply"] = p.hex()
        except Exception as e:  # noqa: BLE001
            rec["greeting_err"] = type(e).__name__
    return rec


async def crawl(seeds, out_path, concurrency, greeting):
    seen = set(seeds)
    queue = asyncio.Queue()
    for s in seeds:
        queue.put_nowait((s, 0))
    sem = asyncio.Semaphore(concurrency)
    out = open(out_path, "w")
    pending = set()

    async def work(ip, depth):
        async with sem:
            rec = await probe(ip, greeting)
        rec["depth_from_seed"] = depth
        out.write(json.dumps(rec) + "\n")
        for peer in rec.get("peers", []):
            pip = peer["ip"]
            if pip not in seen and not pip.startswith(("10.", "172.", "192.168.", "127.")):
                seen.add(pip)
                queue.put_nowait((pip, depth + 1))

    while not queue.empty() or pending:
        while not queue.empty():
            ip, d = queue.get_nowait()
            t = asyncio.create_task(work(ip, d))
            pending.add(t)
            t.add_done_callback(pending.discard)
        if pending:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    out.close()
    return len(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds")
    ap.add_argument("out")
    ap.add_argument("--concurrency", type=int, default=48)
    ap.add_argument("--no-greeting", action="store_true")
    a = ap.parse_args()
    seeds = json.load(open(a.seeds))
    t0 = time.time()
    n = asyncio.run(crawl(seeds, a.out, a.concurrency, not a.no_greeting))
    print(f"probed {n} nodes in {time.time() - t0:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
