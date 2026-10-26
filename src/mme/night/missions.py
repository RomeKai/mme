"""Night missions: complex, pre-defined work that runs unattended while you sleep, after a briefing.

    mme night prep [--only a,b] [--yes]      # BEFORE BED: pre-flight checks + the agent asks you its questions
    mme night run [--until 07:30] [--only a,b] [--dry-run]
    mme night status

Lifecycle:  draft (in missions.yaml) --prep--> briefed (answers stored, spec hash pinned) --run--> PASS/FAIL/...
A mission edited after its briefing needs a new `prep` (the hash no longer matches).

A mission = goal + context + scope + acceptance `verify` + budget, expanded into stages by a template:
  feature  plan 10% -> build 60% -> harden 20% -> review (min 10%, review cap)
  fix      reproduce 20% -> fix 65% -> review
  tests    write 85% -> review
  single   one stage, no review
  custom   your own `stages:` list
The review stage is deliberately cheap: capped at `night.review_max_usd` (budget.json, default $0.30); any surplus
goes to the build stage. Review is read-only: edits outside .mission/ are reverted.

Safety: separate night key + pool, per-stage and per-mission spend kill, per-night cap, worktree + branch per mission
(main untouched, nothing merged), runner-executed verification, scope check, loop breaker, burst watchdog.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from mme import budget, harness, providers
from mme.paths import config_dir, project_root, state_dir
from mme.shell import shell_args

# ---------------------------------------------------------------- templates
TEMPLATES: dict[str, list[dict]] = {
    "feature": [
        {"name": "plan", "share": 0.10, "max_turns": 12, "kind": "plan", "verify": "file:.mission/PLAN.md",
         "instruction": "Read the context and the relevant code. Write .mission/PLAN.md: approach, files you will touch "
                        "(inside SCOPE), the list of tests you will write, and risks. Change nothing else."},
        {"name": "build", "share": 0.60, "max_turns": 45, "kind": "build", "verify": "mission",
         "instruction": "Implement .mission/PLAN.md with TDD: write the tests first, then the code, until DONE WHEN passes."},
        {"name": "harden", "share": 0.20, "max_turns": 25, "kind": "build", "verify": "mission",
         "instruction": "Add edge-case tests for the risks in PLAN.md, fix real bugs they reveal, keep everything green. "
                        "No new features."},
        {"name": "review", "share": 0.10, "max_turns": 8, "kind": "review", "verify": "file:.mission/REVIEW.md",
         "instruction": "Review `git diff {base}...HEAD` like a senior reviewer: correctness, scope, tests, security, "
                        "cost. Write .mission/REVIEW.md: findings ranked blocker/major/minor with file:line, then a "
                        "merge recommendation. Do NOT modify any other file."},
    ],
    "fix": [
        {"name": "reproduce", "share": 0.20, "max_turns": 15, "kind": "build", "verify": None,
         "instruction": "Write a failing test that reproduces the bug described in the goal. Commit it. Don't fix yet."},
        {"name": "fix", "share": 0.65, "max_turns": 35, "kind": "build", "verify": "mission",
         "instruction": "Fix the root cause so the reproducing test and DONE WHEN pass. Smallest correct change."},
        {"name": "review", "share": 0.15, "max_turns": 8, "kind": "review", "verify": "file:.mission/REVIEW.md",
         "instruction": "Review `git diff {base}...HEAD`. Write .mission/REVIEW.md (blocker/major/minor + merge "
                        "recommendation). Do NOT modify any other file."},
    ],
    "tests": [
        {"name": "write", "share": 0.85, "max_turns": 35, "kind": "build", "verify": "mission",
         "instruction": "Write the tests described in the goal. Fix code only for real bugs the tests reveal."},
        {"name": "review", "share": 0.15, "max_turns": 6, "kind": "review", "verify": "file:.mission/REVIEW.md",
         "instruction": "Review `git diff {base}...HEAD`. Write .mission/REVIEW.md. Do NOT modify any other file."},
    ],
    "single": [
        {"name": "do", "share": 1.0, "max_turns": 25, "kind": "build", "verify": "mission",
         "instruction": "Complete the goal until DONE WHEN passes."},
    ],
}

STAGE_PROMPT = """You are executing night mission `{id}`, stage {n}/{total} `{stage}`, UNATTENDED. Nobody can answer questions tonight.

MISSION GOAL
{goal}

DECISIONS MADE WITH THE HUMAN BEFORE BED (follow them; they override your own preferences)
{decisions}

CONTEXT (read first): {context}
SCOPE (the only paths you may create/edit, plus .mission/): {scope}
NOTEBOOK: .mission/PROGRESS.md is shared between stages. Read it (and .mission/PLAN.md if present) first;
at the end of this stage append what you did, what's left, and gotchas.

THIS STAGE: {instruction}
DONE WHEN: {done_when}
BUDGET: about ${budget:.2f} and {turns} turns for this stage. Follow AGENTS.md "Context protocol": targeted reads only.
Finish with: git add -A && git commit -m "night({id}:{stage}): <one-line summary>"

IF BLOCKED (a decision you can't make, a file outside SCOPE, the same error twice): do not guess. Write
.mission/BLOCKED.md (what you tried, the exact error, the decision you need), commit it, and stop.
"""

REPAIR_PROMPT = """Night mission `{id}`, stage `{stage}`: the runner's check FAILED.

CHECK: {done_when}
OUTPUT (tail):
{tail}

Fix the cause within SCOPE ({scope}), run the check yourself, update .mission/PROGRESS.md, commit
("night({id}:{stage}-repair): ..."). Remaining budget about ${budget:.2f}. If you can't, write .mission/BLOCKED.md and stop.
"""

PREP_PROMPT = """You will execute mission `{id}` UNATTENDED tonight; nobody can answer questions then. Ask the human NOW.

GOAL
{goal}

CONTEXT files (read them, read-only): {context}
SCOPE you'll be allowed to edit: {scope}
ACCEPTANCE (runner executes it): {verify}
BUDGET ${budget:.2f} over stages: {stages}

Inspect the context and relevant code (read-only). Then reply with ONLY a JSON object, no prose:
{{"questions": [{{"question": "...", "why": "what changes depending on the answer", "default": "your recommended answer"}}],
 "risks": ["..."], "budget_ok": true, "budget_note": "...", "scope_ok": true, "scope_note": "..."}}
At most 5 questions, only ones whose answer changes what you will build. If everything is clear, return an empty list.
"""


# ---------------------------------------------------------------- model
@dataclass
class Stage:
    name: str
    kind: str
    budget_usd: float
    max_turns: int
    instruction: str
    verify: str | None


@dataclass
class Mission:
    id: str
    goal: str
    verify: str
    budget_usd: float = 2.0
    template: str = "feature"
    context: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    status: str = "draft"
    harness: str = "gateway"
    model: str = ""
    base: str = "main"
    timeout_min: float = 120
    brief: bool = True
    stages_spec: list[dict] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def spec_hash(self) -> str:
        r = {k: v for k, v in self.raw.items() if k != "status"}
        return hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:16]


def expand_stages(m: Mission, review_max: float) -> list[Stage]:
    spec = m.stages_spec if m.template == "custom" else TEMPLATES.get(m.template)
    if spec is None:
        raise ValueError(f"mission {m.id}: unknown template '{m.template}' (use {', '.join([*TEMPLATES, 'custom'])})")
    stages = [Stage(name=s["name"], kind=s.get("kind", "build"), budget_usd=round(m.budget_usd * float(s["share"]), 4),
                    max_turns=int(s.get("max_turns", 25)), instruction=s["instruction"], verify=s.get("verify", "mission"))
              for s in spec]
    surplus = 0.0
    for s in stages:  # cheap review: cap it, give the rest to the biggest build stage
        if s.kind == "review" and s.budget_usd > review_max:
            surplus += s.budget_usd - review_max
            s.budget_usd = review_max
    if surplus:
        builds = [s for s in stages if s.kind == "build"]
        if builds:
            max(builds, key=lambda s: s.budget_usd).budget_usd = round(max(builds, key=lambda s: s.budget_usd).budget_usd + surplus, 4)
    return stages


def missions_file() -> Path:
    return Path(os.environ["MME_NIGHT_MISSIONS"]) if os.environ.get("MME_NIGHT_MISSIONS") else config_dir() / "night" / "missions.yaml"


def night_state_dir() -> Path:
    return Path(os.environ["MME_NIGHT_STATE_DIR"]) if os.environ.get("MME_NIGHT_STATE_DIR") else state_dir() / "night"


def load_missions(path: Path | None = None, review_max: float | None = None) -> list[Mission]:
    import yaml
    doc = yaml.safe_load((path or missions_file()).read_text()) or {}
    defaults = doc.get("defaults", {})
    if review_max is None:
        try:
            review_max = budget.night_settings(budget.load_plan())["review_max_usd"]
        except FileNotFoundError:
            review_max = 0.30
    out, seen = [], set()
    for raw in doc.get("missions", []):
        d = {**defaults, **raw}
        if not d.get("id") or not d.get("goal") or not d.get("verify"):
            raise ValueError(f"mission {d.get('id', '?')}: id, goal and verify are required")
        if d["id"] in seen:
            raise ValueError(f"duplicate mission id {d['id']}")
        seen.add(d["id"])
        m = Mission(id=d["id"], goal=d["goal"].strip(), verify=d["verify"], budget_usd=float(d.get("budget_usd", 2.0)),
                    template=d.get("template", "feature"), context=list(d.get("context", [])),
                    scope=list(d.get("scope", [])), status=d.get("status", "draft"), harness=d.get("harness", "gateway"),
                    model=d.get("model") or providers.harness_model("cheap"), base=d.get("base", "main"),
                    timeout_min=float(d.get("timeout_min", 120)), brief=bool(d.get("brief", True)),
                    stages_spec=list(d.get("stages", [])), raw=raw)
        m.stages = expand_stages(m, review_max)
        out.append(m)
    return out


# ---------------------------------------------------------------- briefs
def brief_path(mid: str) -> Path:
    return night_state_dir() / "briefs" / f"{mid}.json"


def load_brief(m: Mission) -> dict | None:
    p = brief_path(m.id)
    if not p.exists():
        return None
    b = json.loads(p.read_text())
    return b if b.get("hash") == m.spec_hash else None


def is_ready(m: Mission) -> bool:
    return m.status != "skip" and (not m.brief or load_brief(m) is not None)


def decisions_text(m: Mission) -> str:
    b = load_brief(m)
    if not b:
        return "(none: this mission runs without a briefing)"
    lines = [f"- {qa['question']} -> {qa['answer']}" for qa in b.get("answers", [])]
    if b.get("notes"):
        lines.append(f"- Extra notes from the human: {b['notes']}")
    return "\n".join(lines) or "(the agent had no questions)"


# ---------------------------------------------------------------- agent plumbing
def sh(cmd, cwd: Path, timeout: float = 600) -> subprocess.CompletedProcess:
    args, use_shell = shell_args(cmd) if isinstance(cmd, str) else (cmd, False)
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=use_shell)


def agent_env(m: Mission, token_var: str, kind: str, stage: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ANTHROPIC_") and k not in (*harness.ADMIN_SECRETS, "MME_GATEWAY_KEY", "MODEL_API_KEY")}
    env.update(MME_MODE="replay", MME_HARNESS=f"night-{m.harness}", MME_STAGE=stage, MME_STAGE_KIND=kind)
    if m.harness == "gateway":
        for k, v in harness.claude_code_env(harness.GATEWAY_URL, "", m.model).items():
            env[k] = v
        env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get(token_var, "")
    elif m.harness == "muse-code":
        env["META_API_KEY"] = os.environ.get("MODEL_API_KEY", "")
    # harness "claude": ANTHROPIC_* already stripped -> Claude Code uses your own login (Anthropic plan, no Meta credits)
    return env


def agent_cmd(m: Mission, prompt: str, turns: int, readonly: bool) -> list[str]:
    override = os.environ.get("MME_NIGHT_AGENT_CMD")  # tests / custom harness: prompt is the last argument
    if override:
        return [*override.split(), prompt]
    if m.harness in ("gateway", "claude"):
        cmd = ["claude", "-p", prompt, "--max-turns", str(turns), "--output-format", "json"]
        cmd += ["--allowedTools", "Read,Grep,Glob"] if readonly else ["--permission-mode", "acceptEdits"]
        return cmd
    if m.harness == "muse-code":
        return ["muse", "exec", "--max-model-steps", str(turns), "--disable-approval", "--json", prompt]
    raise ValueError(f"unknown harness {m.harness}")


SpendFn = Callable[[], float]


def run_agent(cmd: list[str], cwd: Path, env: dict, spend: SpendFn, max_usd: float, timeout_s: float,
              poll_s: float) -> tuple[str, str, float]:
    """Returns (outcome, output_tail, usd_used). outcome: ok | error | OVER_BUDGET | TIMEOUT."""
    s0, t0 = spend(), time.time()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    outcome = None
    while proc.poll() is None:
        time.sleep(poll_s)
        if spend() - s0 > max_usd:
            outcome = "OVER_BUDGET"
        elif time.time() - t0 > timeout_s:
            outcome = "TIMEOUT"
        if outcome:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
    out = proc.stdout.read() if proc.stdout else ""
    return (outcome or ("ok" if proc.returncode == 0 else "error")), out[-6000:], round(spend() - s0, 4)


def gateway_spend_fn(key_var: str) -> SpendFn:
    http = budget.make_http(harness.GATEWAY_URL, os.environ["LITELLM_MASTER_KEY"])
    key = os.environ[key_var]
    return lambda: budget.key_spend(http, key)


def parse_json_reply(text: str) -> dict:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "questions" in obj:
            return obj
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            text = obj["result"]
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("agent reply has no JSON object")
    return json.loads(text[a:b + 1])


# ---------------------------------------------------------------- prep (briefing)
def preflight(missions: list[Mission], night: dict) -> list[tuple[str, str]]:
    root = project_root()
    checks: list[tuple[str, str]] = []
    dirty = sh(["git", "status", "--porcelain"], root).stdout.strip()
    checks.append(("warn" if dirty else "ok", "uncommitted changes: missions start from the base branch and won't see them"
                   if dirty else "working tree clean"))
    for m in missions:
        if sh(["git", "rev-parse", "--verify", m.base], root).returncode:
            checks.append(("fail", f"{m.id}: base branch '{m.base}' not found"))
        missing = [c for c in m.context if sh(["git", "cat-file", "-e", f"{m.base}:{c}"], root).returncode]
        if missing:
            checks.append(("fail", f"{m.id}: context not committed on {m.base}: {', '.join(missing)}"))
        if not m.scope:
            checks.append(("warn", f"{m.id}: no scope -> the agent may edit anything"))
    total = sum(m.budget_usd for m in missions)
    cap = night["max_per_run_usd"]
    checks.append(("ok" if total <= cap else "warn",
                   f"missions total ${total:.2f} vs per-night cap ${cap:.2f}" + ("" if total <= cap else
                                                                                 " -> the last ones will be skipped")))
    if any(m.harness == "gateway" for m in missions):
        try:
            urllib.request.urlopen(f"{harness.GATEWAY_URL}/health/liveliness", timeout=5)
            checks.append(("ok", "gateway reachable"))
        except Exception:  # noqa: BLE001
            checks.append(("fail", "gateway not reachable: `mme gateway up`"))
        for var in ("MME_NIGHT_KEY", "LITELLM_MASTER_KEY"):
            if not os.environ.get(var):
                checks.append(("fail", f"{var} not set in this shell (use the ops terminal: set -a; source .env)"))
    return checks


def prep(missions: list[Mission], ask: Callable[[str], str] = input, yes: bool = False,
         spend: SpendFn | None = None, now: Callable[[], dt.datetime] = dt.datetime.now) -> int:
    night = budget.night_settings(budget.load_plan()) if _plan_exists() else {"max_per_run_usd": 4.0, "review_max_usd": 0.3}
    todo = [m for m in missions if m.status != "skip"]
    print("== Pre-flight")
    checks = preflight(todo, night)
    for level, msg in checks:
        print(f"  {'✅' if level == 'ok' else '⚠️ ' if level == 'warn' else '❌'} {msg}")
    for m in todo:
        if not m.brief:
            print(f"\n== {m.id}: brief: false -> no questions")
            continue
        if load_brief(m):
            print(f"\n== {m.id}: already briefed (spec unchanged)")
            continue
        print(f"\n== {m.id} ({m.template}, ${m.budget_usd:.2f}): asking the agent what it needs to know...")
        prompt = PREP_PROMPT.format(id=m.id, goal=m.goal, context=", ".join(m.context) or "(none)",
                                    scope=", ".join(m.scope) or "(any)", verify=m.verify, budget=m.budget_usd,
                                    stages=", ".join(f"{s.name} ${s.budget_usd:.2f}" for s in m.stages))
        env = agent_env(m, "MME_GATEWAY_KEY", "prep", "prep")   # briefing spends DAY budget, read-only, small
        sp = spend or ((gateway_spend_fn("MME_GATEWAY_KEY")) if os.environ.get("LITELLM_MASTER_KEY")
                       and os.environ.get("MME_GATEWAY_KEY") else (lambda: 0.0))
        outcome, out, used = run_agent(agent_cmd(m, prompt, 10, readonly=True), project_root(), env, sp,
                                       max_usd=0.25, timeout_s=600, poll_s=float(os.environ.get("MME_NIGHT_POLL_S", "5")))
        try:
            reply = parse_json_reply(out)
        except (ValueError, json.JSONDecodeError):
            print(f"  ❌ couldn't read the agent's questions ({outcome}). Tail:\n{out[-600:]}")
            continue
        answers = []
        for i, q in enumerate(reply.get("questions", [])[:5], 1):
            default = str(q.get("default", "")).strip()
            print(f"\n  Q{i}. {q.get('question')}\n      why: {q.get('why', '-')}")
            a = default if yes else (ask(f"      [Enter = {default}] > ").strip() or default)
            answers.append({"question": q.get("question"), "answer": a})
        for r in reply.get("risks", []):
            print(f"  risk: {r}")
        if reply.get("budget_ok") is False:
            print(f"  ⚠️  agent thinks the budget is short: {reply.get('budget_note', '')}")
        if reply.get("scope_ok") is False:
            print(f"  ⚠️  agent thinks the scope is wrong: {reply.get('scope_note', '')}")
        notes = "" if yes else ask("\n  Anything else it should know? [Enter to skip] > ").strip()
        p = brief_path(m.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hash": m.spec_hash, "answers": answers, "notes": notes,
                                 "risks": reply.get("risks", []), "at": now().isoformat(timespec="minutes"),
                                 "prep_usd": used}, indent=2))
        print(f"  ✅ briefed ({len(answers)} answers, ${used:.2f})")
    ready = [m for m in todo if is_ready(m)]
    fails = [c for c in checks if c[0] == "fail"]
    print(f"\n== Ready to run tonight: {', '.join(m.id for m in ready) or 'none'}")
    if fails:
        print("❌ fix the failed checks before `mme night run`")
        return 1
    print("Next: commit, then `mme night run --until 07:30` (keep Docker up; the computer stays awake).")
    return 0


def _plan_exists() -> bool:
    try:
        budget.load_plan()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- run
def in_scope(path: str, scope: list[str]) -> bool:
    return path.startswith(".mission/") or not scope or any(fnmatch.fnmatch(path, g) for g in scope)


def check(spec: str | None, m: Mission, wt: Path) -> tuple[bool, str]:
    if spec is None:
        return True, "(no check for this stage)"
    if spec.startswith("file:"):
        f = wt / spec[5:]
        return f.exists() and f.stat().st_size > 0, f"{spec[5:]} {'exists' if f.exists() else 'missing'}"
    r = sh(m.verify if spec == "mission" else spec, wt, timeout=900)
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def commit_all(wt: Path, msg: str) -> None:
    if sh(["git", "status", "--porcelain"], wt).stdout.strip():
        sh(["git", "add", "-A"], wt)
        sh(["git", "commit", "-qm", msg], wt)


def run_mission(m: Mission, spend: SpendFn, night_dir: Path, date: str, poll_s: float) -> dict:
    root = project_root()
    branch, wt = f"night/{date}-{m.id}", night_dir / f"{date}-{m.id}"
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], root)
    r = sh(["git", "worktree", "add", "-B", branch, str(wt), m.base], root)
    if r.returncode:
        return {"result": "SETUP_FAILED", "detail": r.stderr[-500:], "branch": branch, "spend": 0.0, "stages": []}
    (wt / ".mission").mkdir(exist_ok=True)
    (wt / ".mission" / "PROGRESS.md").write_text(f"# Mission {m.id}\n\n## Goal\n{m.goal}\n\n## Log\n")

    started, total, stages_out, result = time.time(), 0.0, [], "PASS"
    for n, st in enumerate(m.stages, 1):
        if time.time() - started > m.timeout_min * 60:
            result = "TIMEOUT"
            break
        if total + st.budget_usd > m.budget_usd * 1.1:
            result = "OVER_BUDGET"
            break
        before = sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()
        done_when = m.verify if st.verify == "mission" else (st.verify or "the stage instruction is complete")
        prompt = STAGE_PROMPT.format(id=m.id, n=n, total=len(m.stages), stage=st.name, goal=m.goal,
                                     decisions=decisions_text(m), context=", ".join(m.context) or "(none)",
                                     scope=", ".join(m.scope) or "(any)", instruction=st.instruction.format(base=m.base),
                                     done_when=done_when, budget=st.budget_usd, turns=st.max_turns)
        timeout_s = max(60.0, m.timeout_min * 60 - (time.time() - started))
        outcome, tail, used = run_agent(agent_cmd(m, prompt, st.max_turns, readonly=False), wt,
                                        agent_env(m, "MME_NIGHT_KEY", st.kind, st.name), spend, st.budget_usd,
                                        timeout_s, poll_s)
        total += used
        commit_all(wt, f"night({m.id}:{st.name}): checkpoint by runner")
        reverted = []
        if st.kind == "review":  # read-only stage: undo anything outside .mission/
            changed = sh(["git", "diff", "--name-only", before, "HEAD"], wt).stdout.split()
            reverted = [f for f in changed if not f.startswith(".mission/")]
            if reverted:
                sh(["git", "checkout", before, "--", *reverted], wt)
                commit_all(wt, f"night({m.id}:review): revert edits outside .mission/")
        ok, out = check(st.verify, m, wt)
        if not ok and outcome == "ok" and used < 0.7 * st.budget_usd and not (wt / ".mission/BLOCKED.md").exists():
            rp = REPAIR_PROMPT.format(id=m.id, stage=st.name, done_when=done_when, tail=out[-1200:],
                                      scope=", ".join(m.scope) or "(any)", budget=st.budget_usd - used)
            outcome, tail2, used2 = run_agent(agent_cmd(m, rp, max(5, st.max_turns // 2), readonly=False), wt,
                                              agent_env(m, "MME_NIGHT_KEY", st.kind, st.name + "-repair"), spend,
                                              st.budget_usd - used, timeout_s, poll_s)
            total, used, tail = total + used2, used + used2, tail2
            commit_all(wt, f"night({m.id}:{st.name}-repair): checkpoint by runner")
            ok, out = check(st.verify, m, wt)
        blocked = (wt / ".mission" / "BLOCKED.md").exists()
        sres = ("BLOCKED" if blocked else outcome if outcome in ("OVER_BUDGET", "TIMEOUT") else "PASS" if ok else "FAIL")
        stages_out.append({"stage": st.name, "kind": st.kind, "result": sres, "spend": round(used, 4),
                           "budget": st.budget_usd, "check": out[-800:], "reverted": reverted})
        if sres != "PASS":
            result = sres if st.kind != "review" else "PASS_REVIEW_INCOMPLETE"
            if st.kind != "review":
                break
    final_ok, final_out = check("mission", m, wt)
    if result == "PASS" and not final_ok:
        result = "FAIL"
    changed = sh(["git", "diff", "--name-only", f"{m.base}...{branch}"], root).stdout.split()
    out_of_scope = [f for f in changed if not in_scope(f, m.scope)]
    read = lambda p: (wt / ".mission" / p).read_text()[:3000] if (wt / ".mission" / p).exists() else ""  # noqa: E731
    return {"result": result, "branch": branch, "worktree": str(wt), "spend": round(total, 4),
            "minutes": round((time.time() - started) / 60, 1), "stages": stages_out,
            "diffstat": sh(["git", "diff", "--shortstat", f"{m.base}...{branch}"], root).stdout.strip() or "no changes",
            "out_of_scope": out_of_scope, "final_check": final_out[-1200:], "review": read("REVIEW.md"),
            "blocked": read("BLOCKED.md")}


def report_path() -> Path:
    return Path(os.environ["MME_NIGHT_REPORT"]) if os.environ.get("MME_NIGHT_REPORT") else project_root() / "NIGHT_REPORT.md"


def write_report(date: str, results: list[tuple[Mission, dict]], skipped: list[tuple[str, str]], stop: str) -> None:
    L = [f"# Night report {date}", "", f"Stopped: {stop}", "",
         "| Mission | Result | Spend / budget | Min | Changes | Branch |", "|---|---|---:|---:|---|---|"]
    for m, r in results:
        L.append(f"| {m.id} | **{r['result']}** | ${r.get('spend', 0):.2f} / ${m.budget_usd:.2f} | {r.get('minutes', '-')} | "
                 f"{r.get('diffstat', '-')} | `{r.get('branch', '-')}` |")
    for mid, why in skipped:
        L.append(f"| {mid} | skipped | - | - | {why} | - |")
    L += ["", "## How to review", "```bash", "git log --oneline main..<branch>", "git diff main...<branch>",
          "git merge --no-ff <branch>        # only if you agree", "git worktree remove <worktree> && git branch -D <branch>",
          "```", ""]
    for m, r in results:
        L += [f"## {m.id} — {r['result']}", "", "| Stage | Result | Spend / budget |", "|---|---|---:|"]
        for s in r.get("stages", []):
            L.append(f"| {s['stage']} | {s['result']} | ${s['spend']:.2f} / ${s['budget']:.2f} |")
        if r.get("out_of_scope"):
            L += ["", f"⚠️ **Out of scope:** {', '.join(r['out_of_scope'])}"]
        if any(s.get("reverted") for s in r.get("stages", [])):
            L += ["", "⚠️ Review stage tried to edit code; those edits were reverted."]
        if r.get("blocked"):
            L += ["", "### BLOCKED", "", r["blocked"]]
        if r.get("review"):
            L += ["", "### Review (cheap pass: treat as a checklist, not a verdict)", "", r["review"]]
        L += ["", "<details><summary>final check output</summary>", "", "```", r.get("final_check", "").strip(), "```",
              "</details>", ""]
    report_path().write_text("\n".join(L))


def read_state() -> dict:
    f = night_state_dir() / "state.json"
    return json.loads(f.read_text()) if f.exists() else {"results": {}}


def write_state(s: dict) -> None:
    f = night_state_dir() / "state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(s, indent=2))


def run(args, spend: SpendFn | None = None, now: Callable[[], dt.datetime] = dt.datetime.now) -> int:
    missions = load_missions()
    state = read_state()
    passed = {k for k, v in state["results"].items() if v.get("result", "").startswith("PASS") and v.get("hash")
              and any(m.id == k and m.spec_hash == v["hash"] for m in missions)}
    only = set(args.only.split(",")) if args.only else None
    queue, skipped = [], []
    policy = harness.read_policy()
    for m in missions:
        if m.status == "skip" or (only and m.id not in only) or m.id in passed:
            continue
        if m.harness not in ("gateway", "muse-code", "claude"):
            skipped.append((m.id, f"unknown harness '{m.harness}'"))
            continue
        if not harness.allowed(policy, "claude" if m.harness == "claude" else "gateway" if m.harness == "gateway" else m.harness):
            skipped.append((m.id, f"harness '{m.harness}' not allowed by policy '{policy}'"))
            continue
        if not is_ready(m):
            skipped.append((m.id, "not briefed: run `mme night prep`"))
            continue
        queue.append(m)
    if args.dry_run:
        for m in queue:
            print(f"would run {m.id} ({m.template}, ${m.budget_usd:.2f}): "
                  + " -> ".join(f"{s.name} ${s.budget_usd:.2f}" for s in m.stages))
        for mid, why in skipped:
            print(f"skip {mid}: {why}")
        return 0
    if not queue:
        print("[night] nothing ready" + (f" ({len(skipped)} need `mme night prep`)" if skipped else ""))
        return 0
    if any(m.harness == "gateway" for m in queue) and not os.environ.get("MME_NIGHT_KEY"):
        print("MME_NIGHT_KEY not set (`mme gateway up` mints it)", file=sys.stderr)
        return 2

    watchdog = None
    if spend is None:
        if os.environ.get("MME_NIGHT_KEY") and os.environ.get("LITELLM_MASTER_KEY"):
            spend = gateway_spend_fn("MME_NIGHT_KEY")
            watchdog = subprocess.Popen([sys.executable, "-m", "mme", "watchdog", "--usd", "1.5", "--minutes", "10",
                                         "--poll", "30", "--no-budget-sync"],
                                        env={**os.environ, "MME_GATEWAY_KEY": os.environ["MME_NIGHT_KEY"]},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            spend = lambda: 0.0  # noqa: E731
    night = budget.night_settings(budget.load_plan()) if _plan_exists() else {"max_per_run_usd": 4.0}
    until = None
    if args.until:
        h, mi = map(int, args.until.split(":"))
        until = now().replace(hour=h, minute=mi, second=0, microsecond=0)
        if until <= now():
            until += dt.timedelta(days=1)

    date = now().strftime("%Y%m%d")
    root = project_root()
    night_dir = root.parent / f"{root.name}-night"
    night_dir.mkdir(exist_ok=True)
    harness.log_usage(queue[0].harness, harness.read_policy(), f"night run: {', '.join(m.id for m in queue)}", kind="night")
    results, stop, fails = [], "queue finished", 0
    committed = 0.0  # worst case: each started mission may spend its full budget (it's killed at 110%)
    try:
        for m in queue:
            if until and now() >= until:
                stop = f"--until {args.until} reached"
                break
            if committed + m.budget_usd > night["max_per_run_usd"] + 1e-9:
                skipped.append((m.id, f"would exceed the per-night cap ${night['max_per_run_usd']:.2f}"))
                continue
            print(f"[night] {now():%H:%M} start {m.id} ({len(m.stages)} stages, ${m.budget_usd:.2f})")
            r = run_mission(m, spend, night_dir, date, poll_s=float(os.environ.get("MME_NIGHT_POLL_S", "15")))
            print(f"[night] {now():%H:%M} {m.id}: {r['result']} (${r['spend']:.2f})")
            results.append((m, r))
            committed += max(r["spend"], m.budget_usd)
            state["results"][m.id] = {**r, "date": date, "hash": m.spec_hash}
            write_state(state)
            fails = fails + 1 if not r["result"].startswith("PASS") else 0
            if fails >= 2:
                stop = "2 consecutive failed missions"
                break
    finally:
        if watchdog:
            watchdog.terminate()
    write_report(date, results, skipped, stop)
    print(f"[night] done: {stop}. Report: {report_path()}")
    return 0


def status() -> int:
    state = read_state()
    for m in load_missions():
        r = state["results"].get(m.id) or {}
        ready = "skip" if m.status == "skip" else "briefed" if load_brief(m) else ("no-brief" if not m.brief else "needs prep")
        print(f"{m.id:<28} {m.template:<8} ${m.budget_usd:>5.2f}  {ready:<10} {r.get('result', '-'):<22} {r.get('branch', '')}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mme night")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--only")
    p.add_argument("--yes", action="store_true", help="accept the agent's recommended answers")
    r = sub.add_parser("run")
    r.add_argument("--until")
    r.add_argument("--only")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status()
    if a.cmd == "prep":
        ms = load_missions()
        if a.only:
            ms = [m for m in ms if m.id in a.only.split(",")]
        return prep(ms, yes=a.yes)
    return run(a)


__all__ = ["Mission", "Stage", "TEMPLATES", "load_missions", "prep", "run", "status", "main", "asdict"]
