"""Variable daily budget for the harness key, with rollover and a per-day ceiling.

The harness key in LiteLLM has NO budget_duration: its spend is cumulative over the hackathon.
Every day this module sets the key's max_budget to:

    cap = min( cumulative allowance up to today        (rollover: unused days carry forward),
               spend at start of today + day_max_usd,  (one bad day can't eat the whole surplus),
               total day pool )

Without rollover: cap = spend at start of today + today's allowance.

Before `start`, each day gets `prelaunch_usd` (for kickoff-weekend smoke tests through the gateway).

Plan: <project>/mme/budget.json. State: <project>/.mme/budget_state.json (spend snapshot per day).
    mme budget show     # plan, actual spend per day, today's cap
    mme budget sync     # push today's cap to the gateway (run by `mme use gateway` and the watchdog)
    mme budget caps     # print today's harness cap and night pool (used by `mme gateway up`)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Callable

from mme.paths import config_dir, state_dir


def plan_file() -> Path:
    return Path(os.environ["MME_BUDGET_PLAN"]) if os.environ.get("MME_BUDGET_PLAN") else config_dir() / "budget.json"


def state_file() -> Path:
    return Path(os.environ["MME_BUDGET_STATE"]) if os.environ.get("MME_BUDGET_STATE") else state_dir() / "budget_state.json"


def load_plan(path: Path | None = None) -> dict:
    return json.loads((path or plan_file()).read_text())


def days(plan: dict) -> list[dt.date]:
    s, e = dt.date.fromisoformat(plan["start"]), dt.date.fromisoformat(plan["end"])
    return [s + dt.timedelta(i) for i in range((e - s).days + 1)]


def allowance(plan: dict, day: dt.date) -> float:
    """Planned harness spend for one calendar day (0 outside the hackathon window)."""
    if day not in days(plan):
        return 0.0
    o = plan.get("overrides", {}).get(day.isoformat())
    if o is not None:
        return float(o)
    return float(plan["weekend_usd"] if day.weekday() >= 5 else plan["weekday_usd"])


def day_pool(plan: dict) -> float:
    return round(sum(allowance(plan, d) for d in days(plan)), 4)


def validate(plan: dict) -> list[str]:
    problems = []
    total = float(plan["total_harness_usd"])
    night = night_settings(plan)["pool_usd"]
    if day_pool(plan) + night > total + 1e-9:
        problems.append(f"day plan ${day_pool(plan):.2f} + night pool ${night:.2f} > total ${total:.2f}")
    if float(plan["day_max_usd"]) < max(allowance(plan, d) for d in days(plan)):
        problems.append("day_max_usd is lower than some day's allowance")
    return problems


def cap_for(plan: dict, today: dt.date, spent_at_day_start: float) -> float:
    if today < dt.date.fromisoformat(plan["start"]):
        return round(spent_at_day_start + float(plan.get("prelaunch_usd", 0.0)), 4)
    if not plan.get("rollover", True):
        return round(spent_at_day_start + allowance(plan, today), 4)
    cumulative = sum(allowance(plan, d) for d in days(plan) if d <= today)
    return round(min(cumulative, spent_at_day_start + float(plan["day_max_usd"]), day_pool(plan)), 4)


def snapshot(state: dict, today: dt.date, spent_now: float) -> tuple[float, dict]:
    """Spend at the start of `today`, recorded the first time we look at the key that day."""
    key = today.isoformat()
    snaps = dict(state.get("day_start_spend", {}))
    if key not in snaps:
        snaps[key] = spent_now
    return snaps[key], {**state, "day_start_spend": snaps}


def read_state() -> dict:
    f = state_file()
    return json.loads(f.read_text()) if f.exists() else {}


def write_state(state: dict) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2))


def night_settings(plan: dict) -> dict:
    n = plan.get("night", {})
    return {
        "pool_usd": float(n.get("pool_usd", plan.get("night_pool_usd", 0.0))),
        "max_per_run_usd": float(n.get("max_per_run_usd", plan.get("night_max_per_run_usd", 4.0))),
        "review_max_usd": float(n.get("review_max_usd", 0.30)),
    }


# ---------- gateway I/O ----------
Http = Callable[[str, str, dict | None], dict]


def make_http(base: str, master: str) -> Http:
    def call(method: str, path: str, body: dict | None) -> dict:
        req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {master}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    return call


def key_spend(http: Http, key: str) -> float:
    info = http("GET", f"/key/info?key={key}", None)
    return float((info.get("info") or info).get("spend") or 0.0)


def sync(http: Http, key: str, plan: dict, today: dt.date, state: dict) -> tuple[float, float, dict]:
    spent = key_spend(http, key)
    start, state = snapshot(state, today, spent)
    cap = cap_for(plan, today, start)
    http("POST", "/key/update", {"key": key, "max_budget": cap})
    return spent, cap, {**state, "last_sync": dt.datetime.now().isoformat(timespec="seconds"), "last_cap": cap}


# ---------- CLI ----------
def show(plan: dict, state: dict, today: dt.date) -> str:
    snaps = state.get("day_start_spend", {})
    lines = [f"{'day':<12}{'plan':>7}{'cum plan':>10}{'spent':>9}", "-" * 38]
    cum = 0.0
    ds = days(plan)
    for i, d in enumerate(ds):
        cum += allowance(plan, d)
        nxt = snaps.get(ds[i + 1].isoformat()) if i + 1 < len(ds) else None
        here = snaps.get(d.isoformat())
        spent = f"{nxt - here:9.2f}" if (here is not None and nxt is not None) else ("    today" if d == today else "")
        mark = " <" if d == today else ""
        lines.append(f"{d.isoformat()} {d.strftime('%a')}{allowance(plan, d):6.2f}{cum:10.2f}{spent}{mark}")
    lines.append("-" * 38)
    lines.append(f"day pool ${day_pool(plan):.2f} + night pool ${night_settings(plan)['pool_usd']:.2f} "
                 f"of ${plan['total_harness_usd']:.2f}")
    if "last_cap" in state:
        lines.append(f"key cap now ${state['last_cap']:.2f} (synced {state.get('last_sync')})")
    for p in validate(plan):
        lines.append(f"WARNING: {p}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    try:
        plan = load_plan()
    except FileNotFoundError:
        print(f"[budget] no plan at {plan_file()} (run `mme init`)", file=sys.stderr)
        return 0 if "--quiet" in argv else 2
    today = dt.date.fromisoformat(os.environ["MME_TODAY"]) if os.environ.get("MME_TODAY") else dt.date.today()
    cmd = argv[0] if argv else "show"
    state = read_state()
    if cmd == "show":
        print(show(plan, state, today))
        return 0
    if cmd == "caps":
        start = state.get("day_start_spend", {}).get(today.isoformat(), 0.0)
        print(f"{max(cap_for(plan, today, start), 0.01):.2f} {night_settings(plan)['pool_usd']:.2f}")
        return 0
    if cmd == "sync":
        key, master = os.environ.get("MME_GATEWAY_KEY"), os.environ.get("LITELLM_MASTER_KEY")
        quiet = "--quiet" in argv
        if not key or not master:
            if not quiet:
                print("[budget] MME_GATEWAY_KEY / LITELLM_MASTER_KEY not set; skipped", file=sys.stderr)
            return 0
        try:
            http = make_http(os.environ.get("MME_GATEWAY_URL", "http://127.0.0.1:4000"), master)
            spent, cap, state = sync(http, key, plan, today, state)
            write_state(state)
            print(f"[budget] {today} {today.strftime('%a')}: plan ${allowance(plan, today):.2f}, "
                  f"key cap ${cap:.2f}, spent so far ${spent:.2f} -> ${max(cap - spent, 0):.2f} left today",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - never block work because the sync failed
            print(f"[budget] sync failed ({e}); the previous cap stays in force", file=sys.stderr)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


