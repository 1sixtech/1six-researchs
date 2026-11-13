#!/usr/bin/env python3
"""Drop an upstream peer that cannot keep up, and let the node move to the next one in its list.

Every --interval seconds it reads the node's upstream block-stream connection (established TCP connection to a
remote port 4001) and its received-byte counter, and the node's lag from its own output (local_time minus
block_time of the latest streamed line). If the node is more than --max-lag-s behind while the incoming
rate is below --min-mbps (too slow to catch up) for --grace seconds, it removes that peer from override_gossip_config.json and restarts the node
service. Events are appended as JSON lines to --log.

Needs: the node run with --stream-with-block-info and an output stream such as --write-trades, a systemd unit
(--service), and `ss` (iproute2). Run as a user that may restart the unit (sudo).
Usage: python3 peer_watchdog.py --service nonval [--min-mbps 1.0] [--max-lag-s 2] [--grace 30]
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import time


def upstream():
    out = subprocess.run(["ss", "-tin", "state", "established", "( dport = :4001 )"],
                         capture_output=True, text=True).stdout
    m = re.search(r"\s(\d+\.\d+\.\d+\.\d+):4001\s.*?bytes_received:(\d+)", out, re.S)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def node_lag(data_dir):
    files = glob.glob(os.path.join(data_dir, "node_trades_streaming/hourly/*/*"))
    if not files:
        return None
    f = max(files, key=os.path.getmtime)
    with open(f, "rb") as fh:
        fh.seek(max(0, os.path.getsize(f) - 20000))
        lines = fh.read().decode(errors="ignore").splitlines()[1:]
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = lambda s: dt.datetime.strptime(s[:26], "%Y-%m-%dT%H:%M:%S.%f")
        written = p(d["local_time"])
        # lag of the latest line, plus how long ago it was written (catches a stalled stream)
        return (written - p(d["block_time"])).total_seconds() + (dt.datetime.utcnow() - written).total_seconds()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="nonval")
    ap.add_argument("--config", default=os.path.expanduser("~/override_gossip_config.json"))
    ap.add_argument("--data", default=os.path.expanduser("~/hl/data"))
    ap.add_argument("--min-mbps", type=float, default=1.0, help="MB/s; a live stream needs about 1.5 to 2")
    ap.add_argument("--max-lag-s", type=float, default=2.0)
    ap.add_argument("--grace", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--log", default=os.path.expanduser("~/peer_watchdog.jsonl"))
    a = ap.parse_args()
    log = lambda **k: open(a.log, "a").write(json.dumps({"t": time.time(), **k}) + "\n")
    last_ip, last_b, last_t, bad_since = None, None, None, None
    log(event="start")
    while True:
        time.sleep(a.interval)
        ip, b = upstream()
        now = time.time()
        lag = node_lag(a.data)
        if ip is None or ip != last_ip:
            if ip != last_ip:
                log(event="upstream", ip=ip)
            last_ip, last_b, last_t, bad_since = ip, b, now, None
            continue
        mbps = (b - last_b) / (now - last_t) / 1e6
        last_b, last_t = b, now
        # bad = behind AND the stream is too slow to catch up (a slow machine with a fast stream is not the peer's fault)
        if lag is not None and lag > a.max_lag_s and mbps < a.min_mbps:
            bad_since = bad_since or now
            if now - bad_since >= a.grace:
                cfg = json.load(open(a.config))
                cfg["root_node_ips"] = [x for x in cfg["root_node_ips"] if x["Ip"] != ip]
                json.dump(cfg, open(a.config, "w"))
                log(event="drop", ip=ip, mbps=round(mbps, 3), lag_s=None if lag is None else round(lag, 2))
                subprocess.run(["sudo", "systemctl", "restart", a.service])
                last_ip, bad_since = None, None
        else:
            bad_since = None


if __name__ == "__main__":
    main()
