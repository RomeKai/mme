"""Delivery discipline: milestones with executable gates, a daily status board, and a ship checklist.

    mme today            countdown, today's goal, what's behind (and what to cut), budget, last night
    mme gate [--day D | --all]   run gate commands, record results
    mme ship             submission checklist (tests, README, demo link, secrets, deploy URL, AI usage log...)

Config: <project>/mme/milestones.yaml. Results: <project>/.mme/gates.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from mme.paths import config_dir, project_root, state_dir


def load(path: Path | None = None) -> dict:
    import yaml
    p = path or (Path(os.environ["MME_MILESTONES"]) if os.environ.get("MME_MILESTONES") else config_dir() / "milestones.yaml")
    return yaml.safe_load(p.read_text()) or {}


def results_file() -> Path:
    return state_dir() / "gates.json"


def read_results() -> dict:
    f = results_file()
    return json.loads(f.read_text()) if f.exists() else {}


def write_results(r: dict) -> None:
    f = results_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(r, indent=2))


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def run_cmd(cmd: str, timeout: int = 900) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=project_root(), capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "MME_MODE": os.environ.get("MME_MODE", "replay")})
        return r.returncode == 0, (r.stdout + r.stderr)[-800:]
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"


# ---------------------------------------------------------------- gates
def run_gates(cfg: dict, days: list[str]) -> tuple[int, int]:
    res = read_results()
    ok_n = fail_n = 0
    for ms in cfg.get("milestones", []):
        if str(ms["day"]) not in days:
            continue
        print(f"\n{ms['day']}  {ms.get('goal', '')}")
        for g in ms.get("gates", []):
            ok, out = run_cmd(g["run"])
            res.setdefault(str(ms["day"]), {})[g["name"]] = {"ok": ok, "at": dt.datetime.now().isoformat(timespec="minutes")}
            print(f"  {'✅' if ok else '❌'} {g['name']}")
            if not ok:
                print("     " + out.strip().replace("\n", "\n     ")[-500:])
            ok_n, fail_n = ok_n + ok, fail_n + (not ok)
    write_results(res)
    return ok_n, fail_n


def gate_status(cfg: dict, today: dt.date) -> tuple[list[dict], list[dict]]:
    """(behind, today_items). Behind = a milestone before today with a gate that failed or never ran."""
    res = read_results()
    behind, todays = [], []
    for ms in cfg.get("milestones", []):
        d = dt.date.fromisoformat(str(ms["day"]))
        gates = ms.get("gates", [])
        state = {g["name"]: res.get(str(ms["day"]), {}).get(g["name"], {}).get("ok") for g in gates}
        item = {**ms, "state": state}
        if d < today and any(v is not True for v in state.values()):
            behind.append(item)
        if d == today:
            todays.append(item)
    return behind, todays


# ---------------------------------------------------------------- today
def today_board(cfg: dict, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now().astimezone()
    today = now.date()
    L = []
    internal = parse_ts(cfg["internal_deadline"]) if cfg.get("internal_deadline") else None
    deadline = parse_ts(cfg["deadline"]) if cfg.get("deadline") else None
    if internal:
        left = internal - now
        h = int(left.total_seconds() // 3600)
        L.append(f"⏳ {h // 24}d {h % 24}h to the internal deadline ({internal:%a %d %b %H:%M %Z})"
                 + (f" · official {deadline:%a %d %b %H:%M %Z}" if deadline else ""))
    freeze = cfg.get("freeze")
    if freeze and today >= dt.date.fromisoformat(str(freeze)):
        L.append("🧊 FEATURE FREEZE: only fixes, polish, demo assets. New ideas -> docs/CUTLIST.md")
    behind, todays = gate_status(cfg, today)
    for ms in todays:
        L.append(f"\n📌 Today: {ms.get('goal', '')}")
        for name, ok in ms["state"].items():
            L.append(f"   {'✅' if ok else '❌' if ok is False else '⬜'} {name}")
    if not todays:
        nxt = [m for m in cfg.get("milestones", []) if dt.date.fromisoformat(str(m["day"])) > today]
        if nxt:
            L.append(f"\n📌 Next milestone {nxt[0]['day']}: {nxt[0].get('goal', '')}")
    if behind:
        L.append("\n🚨 BEHIND:")
        for ms in behind:
            bad = [n for n, ok in ms["state"].items() if ok is not True]
            L.append(f"   {ms['day']} {ms.get('goal', '')}: {', '.join(bad)}")
            for c in ms.get("cut_if_late", []):
                L.append(f"      ✂ consider cutting: {c}")
    else:
        L.append("\n🟢 On track (all past gates green)")
    try:
        from mme import budget
        plan = budget.load_plan()
        st = budget.read_state()
        start = dt.date.fromisoformat(plan["start"])
        allow = budget.allowance(plan, today) if today >= start else float(plan.get("prelaunch_usd", 0.0))
        L.append(f"\n💵 Today's harness plan ${allow:.2f}" + (" (prelaunch)" if today < start else "")
                 + (f" · key cap ${st['last_cap']:.2f} (synced {st.get('last_sync', '?')})" if "last_cap" in st else ""))
    except (FileNotFoundError, KeyError):
        pass
    try:
        from mme.night import missions
        s = missions.read_state()["results"]
        last = sorted(s.items(), key=lambda kv: kv[1].get("date", ""))[-3:]
        if last:
            L.append("🌙 Last night: " + ", ".join(f"{k} {v['result']}" for k, v in last) + "  (NIGHT_REPORT.md)")
    except Exception:  # noqa: BLE001
        pass
    L.append("\nNext: `mme gate` when you think today's goal is done · `mme night prep` before bed")
    return "\n".join(L)


def short_line(cfg: dict) -> str:
    now = dt.datetime.now().astimezone()
    behind, todays = gate_status(cfg, now.date())
    parts = []
    if cfg.get("internal_deadline"):
        h = int((parse_ts(cfg["internal_deadline"]) - now).total_seconds() // 3600)
        parts.append(f"{h // 24}d{h % 24}h to internal deadline")
    if todays:
        parts.append(f"today: {todays[0].get('goal', '')}")
    if behind:
        parts.append(f"BEHIND on {len(behind)} milestone(s): {', '.join(str(b['day']) for b in behind)}")
    return "[MME] " + " · ".join(parts) if parts else ""


# ---------------------------------------------------------------- ship
SECRET_RE = r"sk-[A-Za-z0-9_-]{16,}|MODEL_API_KEY=[A-Za-z0-9_-]{8,}|LITELLM_MASTER_KEY=sk-[A-Za-z0-9_-]{8,}"


def ship_checks(cfg: dict) -> list[tuple[str, bool, str]]:
    root = project_root()
    ship = cfg.get("ship", {})
    out: list[tuple[str, bool, str]] = []

    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)

    dirty = git("status", "--porcelain").stdout.strip()
    out.append(("git: everything committed", not dirty, dirty[:200]))
    up = git("rev-list", "--count", "@{u}..HEAD")
    out.append(("git: pushed to remote", up.returncode == 0 and up.stdout.strip() == "0",
                "no upstream" if up.returncode else f"{up.stdout.strip()} unpushed commits"))
    if cfg.get("test_cmd"):
        ok, o = run_cmd(cfg["test_cmd"])
        out.append(("tests pass (replay mode, $0)", ok, o[-300:]))
    readme = root / ship.get("readme", "README.md")
    text = readme.read_text() if readme.exists() else ""
    out.append(("README exists", bool(text), str(readme)))
    out.append(("README names the Meta models used", bool(re.search(r"muse|meta model api|sam 3", text, re.I)), ""))
    out.append(("README has a demo video link", bool(re.search(ship.get("demo_url_pattern", r"youtu|loom|vimeo|drive\.google"),
                                                                text, re.I)), ""))
    out.append(("README has run instructions (code block)", "```" in text, ""))
    out.append(("LICENSE present", (root / "LICENSE").exists(), ""))
    out.append(("AI usage log present (docs/AI_USAGE.md)", (root / "docs" / "AI_USAGE.md").exists(), ""))
    leaks = git("grep", "-nIE", SECRET_RE).stdout.strip()
    out.append(("no secrets in tracked files", not leaks, leaks[:300]))
    rd = root / ship.get("replay_dir", "samples/replay")
    out.append((f"offline demo cache present ({rd.relative_to(root)})", rd.exists() and any(f.is_file() and not f.name.startswith(".") for f in rd.rglob("*")), ""))
    if ship.get("deploy_url"):
        try:
            code = urllib.request.urlopen(ship["deploy_url"], timeout=15).status
            out.append((f"deploy URL responds ({ship['deploy_url']})", code == 200, str(code)))
        except Exception as e:  # noqa: BLE001
            out.append((f"deploy URL responds ({ship['deploy_url']})", False, str(e)[:200]))
    else:
        out.append(("deploy URL set in milestones.yaml (ship.deploy_url)", False, "empty"))
    behind, _ = gate_status(cfg, dt.date.today() + dt.timedelta(days=1))
    out.append(("all milestone gates green", not behind, ", ".join(str(b["day"]) for b in behind)))
    for x in ship.get("extra", []):
        ok, o = run_cmd(x["run"])
        out.append((x["name"], ok, o[-200:]))
    return out


def main(argv: list[str], cmd: str) -> int:
    cfg = load()
    if cmd == "today":
        if "--short" in argv:
            line = short_line(cfg)
            if line:
                print(line)
            return 0
        print(today_board(cfg))
        return 0
    if cmd == "gate":
        ap = argparse.ArgumentParser(prog="mme gate")
        ap.add_argument("--day")
        ap.add_argument("--all", action="store_true")
        a = ap.parse_args(argv)
        today = dt.date.today().isoformat()
        days = [str(m["day"]) for m in cfg.get("milestones", [])] if a.all else [a.day or today]
        if not a.all and not a.day:  # default: today + anything behind
            behind, _ = gate_status(cfg, dt.date.today())
            days += [str(b["day"]) for b in behind]
        ok, bad = run_gates(cfg, days)
        print(f"\n{ok} passed, {bad} failed")
        return 1 if bad else 0
    if cmd == "ship":
        checks = ship_checks(cfg)
        print("== Ship checklist")
        for name, ok, detail in checks:
            print(f"  {'✅' if ok else '❌'} {name}" + (f"\n       {detail}" if (detail and not ok) else ""))
        bad = sum(not ok for _, ok, _ in checks)
        print(f"\n{'READY TO SUBMIT' if not bad else f'{bad} item(s) to fix before submitting'}")
        return 1 if bad else 0
    return 2
