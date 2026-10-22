"""Spend anomaly watchdog for the MME gateway (run it in a spare terminal while you work).

Polls LiteLLM for the harness key's spend. If spend inside the sliding window exceeds the
threshold (a runaway loop or subagent storm), it rings the bell, prints an alert and BLOCKS the key.
Unblock after you've looked:  mme watchdog --unblock

    mme watchdog                       # defaults: >$1.50 within 10 min -> block
    mme watchdog --usd 0.8 --minutes 5 --no-block

Env: MME_GATEWAY_KEY, LITELLM_MASTER_KEY, MME_GATEWAY_URL (default http://127.0.0.1:4000)
Uses LiteLLM /key/info, /key/block, /key/unblock (check.sh verifies /key/info on day 0).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import deque
from typing import Callable

Http = Callable[[str, str, dict | None], dict]


def make_http(base: str, master: str) -> Http:
    def call(method: str, path: str, body: dict | None) -> dict:
        req = urllib.request.Request(
            base.rstrip("/") + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {master}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    return call


def key_spend(http: Http, key: str) -> float:
    info = http("GET", f"/key/info?key={key}", None)
    return float((info.get("info") or info).get("spend") or 0.0)


class Watchdog:
    def __init__(self, threshold_usd: float, window_s: float):
        self.threshold = threshold_usd
        self.window = window_s
        self.samples: deque[tuple[float, float]] = deque()

    def observe(self, t: float, spend: float) -> float | None:
        """Add a sample; return the spend within the window if it breaches the threshold."""
        # Daily budget reset makes spend drop: start over.
        if self.samples and spend < self.samples[-1][1]:
            self.samples.clear()
        self.samples.append((t, spend))
        while self.samples and t - self.samples[0][0] > self.window:
            self.samples.popleft()
        burst = spend - self.samples[0][1]
        return burst if burst > self.threshold else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mme watchdog")
    ap.add_argument("--usd", type=float, default=1.5)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--poll", type=float, default=30, help="seconds between polls")
    ap.add_argument("--no-block", action="store_true")
    ap.add_argument("--unblock", action="store_true")
    ap.add_argument("--no-budget-sync", action="store_true",
                    help="don't move the daily cap (used when watching the night key)")
    a = ap.parse_args(argv)

    key, master = os.environ.get("MME_GATEWAY_KEY"), os.environ.get("LITELLM_MASTER_KEY")
    if not key or not master:
        sys.exit("set MME_GATEWAY_KEY and LITELLM_MASTER_KEY (source .env)")
    http = make_http(os.environ.get("MME_GATEWAY_URL", "http://127.0.0.1:4000"), master)

    if a.unblock:
        http("POST", "/key/unblock", {"key": key})
        print("key unblocked")
        return 0

    dog = Watchdog(a.usd, a.minutes * 60)
    last_day = None

    print(f"[watchdog] alert if > ${a.usd:.2f} within {a.minutes:g} min (poll {a.poll:g}s). Ctrl-C to stop.")
    while True:
        try:
            today = time.strftime("%Y-%m-%d")
            if not a.no_budget_sync and today != last_day:   # new day -> new cap (variable plan + rollover)
                from mme import budget
                budget.main(["sync"])
                last_day = today
            spend = key_spend(http, key)
            burst = dog.observe(time.time(), spend)
            print(f"\r[watchdog] {time.strftime('%H:%M:%S')} key spend today ${spend:.3f}   ", end="", flush=True)
            if burst is not None:
                print(f"\a\n[watchdog] ANOMALY: ${burst:.2f} in the last {a.minutes:g} min.")
                if not a.no_block:
                    http("POST", "/key/block", {"key": key})
                    print("[watchdog] key BLOCKED. Inspect the session, then: mme watchdog --unblock")
                    return 1
                dog.samples.clear()
        except Exception as e:  # noqa: BLE001 - keep watching through transient errors
            print(f"\n[watchdog] poll error: {e}")
        time.sleep(a.poll)


