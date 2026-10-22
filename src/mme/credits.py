"""One view of ALL credit spend: product ledger + gateway keys (day and night) + manually logged extras.

    mme credits                   # full report
    mme credits --short           # one line (session-start hook)
    mme credits --add-harness X   # spend the gateway can't see (meta-direct, Muse Code), read from the console
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

from mme.paths import state_dir

TOTAL = float(os.environ.get("MME_TOTAL_CREDITS_USD", "150"))


def ledger_rows() -> list[dict]:
    f = state_dir() / "ledger.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def gateway_spend() -> dict[str, float]:
    """Spend per gateway key. Empty if the gateway or the master key isn't available (never raises)."""
    master = os.environ.get("LITELLM_MASTER_KEY")
    keys = {"harness (day)": os.environ.get("MME_GATEWAY_KEY"), "harness (night)": os.environ.get("MME_NIGHT_KEY")}
    if not master:
        return {}
    from mme import budget
    http = budget.make_http(os.environ.get("MME_GATEWAY_URL", "http://127.0.0.1:4000"), master)
    out = {}
    for name, key in keys.items():
        if key:
            try:
                out[name] = budget.key_spend(http, key)
            except Exception:  # noqa: BLE001
                pass
    return out


def summary() -> dict:
    rows = ledger_rows()
    product = sum(r.get("cost_usd", 0.0) for r in rows if r.get("tag") != "harness")
    manual = sum(r.get("cost_usd", 0.0) for r in rows if r.get("tag") == "harness")
    gw = gateway_spend()
    spent = product + manual + sum(gw.values())
    return {"product": product, "manual_harness": manual, "gateway": gw, "spent": spent, "left": TOTAL - spent,
            "rows": rows, "gateway_seen": bool(gw)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mme credits")
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--add-harness", type=float, metavar="USD")
    a = ap.parse_args(argv)
    if a.add_harness is not None:
        f = state_dir() / "ledger.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": "harness",
                                 "cost_usd": a.add_harness, "tag": "harness"}) + "\n")
    s = summary()
    if a.short:
        src = "product + gateway" if s["gateway_seen"] else "product ledger only (gateway not visible here)"
        print(f"[MME] credits: ${s['spent']:.2f} spent / ${TOTAL:.0f} -> ${s['left']:.2f} left ({src})")
        return 0
    print(f"Spent ${s['spent']:.2f} of ${TOTAL:.0f}  ->  ${s['left']:.2f} left\n")
    print(f"  product (llm client)      ${s['product']:8.3f}")
    for k, v in s["gateway"].items():
        print(f"  {k:<25} ${v:8.3f}")
    if not s["gateway_seen"]:
        print("  gateway                   (not visible: run in the ops terminal with LITELLM_MASTER_KEY)")
    print(f"  harness, logged by hand   ${s['manual_harness']:8.3f}")

    by_model, by_tag = defaultdict(float), defaultdict(float)
    inp = cached = 0
    for r in s["rows"]:
        by_model[r["model"]] += r.get("cost_usd", 0.0)
        by_tag[r.get("tag", "?")] += r.get("cost_usd", 0.0)
        inp += r.get("input_tokens", 0)
        cached += r.get("cached_tokens", 0)
    if inp:
        print(f"\nPrompt-cache hit ratio (product): {cached / inp:.0%}  (aim > 50% on repeated pipelines)")
    for title, d in (("Product by model", by_model), ("Product by tag", by_tag)):
        if d:
            print(f"\n{title}:")
            for k, v in sorted(d.items(), key=lambda kv: -kv[1]):
                print(f"  {k:<32} ${v:8.3f}")
    return 0
