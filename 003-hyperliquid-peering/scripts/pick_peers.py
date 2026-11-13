#!/usr/bin/env python3
"""Pick upstream peers for a Hyperliquid non-validating node and write override_gossip_config.json.

Run it on the node's own machine (so connect times reflect your location):
  1. collect the official root peers (API `gossipRootIps` + the validator-community table in the node README),
  2. crawl the gossip network from them (`Peers` on port 4002),
  3. sample every synced node's block height together for ~1 minute (`GossipStatus`),
  4. rank candidates and write the config: root peers first (in case one has a free slot), then the best
     candidates, with try_new_peers false.

Candidate rules (from our crawl of 2026-11-13): one hop from a root peer; under --max-connect-ms from this machine;
under --max-lag-ms behind the root peers; serving fewer than --max-children nodes (8 looks like the default limit).
Upstream bandwidth cannot be seen from outside; run peer_watchdog.py to drop a peer that cannot keep up.

Usage: python3 pick_peers.py [--out ~/override_gossip_config.json] [--top 12] [--max-connect-ms 5]
"""
import argparse
import asyncio
import json
import os
import re
import statistics as S
import time
import urllib.request

from crawl_gossip import crawl
from sample_status import main_async as sample

README = "https://raw.githubusercontent.com/hyperliquid-dex/node/main/README.md"
API = "https://api.hyperliquid.xyz/info"


def root_peers():
    req = urllib.request.Request(API, data=b'{"type":"gossipRootIps"}', headers={"Content-Type": "application/json"})
    api = json.load(urllib.request.urlopen(req, timeout=10))
    text = urllib.request.urlopen(README, timeout=10).read().decode()
    table = re.findall(r"\|\s*[^|]+?\s*\|\s*(\d+\.\d+\.\d+\.\d+)\s*\|\s*[^|]+?\s*\|", text)
    return sorted(set(api) | set(table))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/override_gossip_config.json"))
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--max-connect-ms", type=float, default=5.0)
    ap.add_argument("--max-lag-ms", type=float, default=200.0)
    ap.add_argument("--max-children", type=int, default=8)
    ap.add_argument("--exclude", nargs="*", default=[], help="IPs to leave out (e.g. peers the watchdog dropped)")
    ap.add_argument("--workdir", default=os.path.expanduser("~/peer-picker"))
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    t = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    seeds = root_peers()
    crawl_path = os.path.join(a.workdir, f"crawl-{t}.jsonl")
    asyncio.run(crawl(seeds, crawl_path, 48, False))
    nodes = {r["ip"]: r for r in map(json.loads, open(crawl_path))}
    synced = [ip for ip, r in nodes.items() if r.get("status")]
    status_path = os.path.join(a.workdir, f"status-{t}.jsonl")
    asyncio.run(sample(synced, status_path, 30, 2.0))

    # hops and lag, same method as the article (analyze_crawl.py)
    from analyze_crawl import hops_from, lags
    children = {ip: [p["ip"] for p in r.get("peers", [])] for ip, r in nodes.items()}
    hop = hops_from(set(seeds), children)
    rate, med = lags([json.loads(x) for x in open(status_path)], set(seeds))
    ms = 1000 / rate
    cands = []
    for ip, r in nodes.items():
        if ip in seeds or ip in a.exclude or hop.get(ip) != 1 or ip not in med or "peers" not in r:
            continue
        lag_ms = med[ip] * ms
        if (r.get("connect_ms", 1e9) <= a.max_connect_ms and lag_ms <= a.max_lag_ms
                and 0 < len(children[ip]) < a.max_children):
            cands.append((lag_ms, r["connect_ms"], ip, len(children[ip])))
    cands.sort()
    pick = [ip for _, _, ip, _ in cands[:a.top]]
    cfg = {"root_node_ips": [{"Ip": ip} for ip in seeds + pick], "try_new_peers": False,
           "chain": "Mainnet", "reserved_peer_ips": []}
    json.dump(cfg, open(a.out, "w"))
    print(f"{len(nodes)} nodes, {len(synced)} synced, {len(seeds)} root peers, {len(cands)} candidates")
    for lag_ms, cms, ip, nch in cands[:a.top]:
        print(f"  {ip:16s} {lag_ms:6.0f} ms behind root peers  connect {cms:5.2f} ms  serves {nch}")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
