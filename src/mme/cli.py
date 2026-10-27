"""mme — budget-safe agent harness kit for the Meta Model API (on top of ECC).

Setup
  mme init [--start YYYY-MM-DD]     install MME files into this repo
  mme doctor                        check tools, config, keys, hooks
  eval "$(mme shell-init)"          enable `mme use` / `mme fallback` in your shell (add to ~/.bashrc / ~/.zshrc)
Harness (agent terminal)
  mme status | use <harness> [why] | fallback [why] | policy <meta-only|meta-first|open>
Daily (ops terminal: has the admin secrets)
  mme today | gate [--day D|--all] | ship
  mme budget [show|sync|caps] | credits [--short|--add-harness X] | watchdog [...]
  mme night prep [--yes] | night run [--until HH:MM] [--dry-run] | night status
Gateway & tests
  mme gateway up [--openai] | check | drill | backup | render [--openai|--mock] | down
  mme smoke [--image] | mock [--port N] | models
Hooks (called by .claude/settings.json)
  mme hook session-start | loop-breaker | announce-guard
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from mme import __version__
from mme.paths import PACKAGE_DIR, config_dir, project_root, state_dir


def _load_dotenv(env: dict) -> dict:
    f = project_root() / ".env"
    if not f.exists():
        return env
    out = dict(env)
    for line in f.read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if v.strip() and k.strip() not in out:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_bash() -> str | None:
    """bash for the gateway scripts. On Windows, skip the WSL launcher (System32) and fall back to Git Bash."""
    bash = shutil.which("bash")
    if os.name != "nt" or (bash and Path(bash).parent.name.lower() not in ("system32", "windowsapps")):
        return bash
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"  # <Git>/cmd/git.exe -> <Git>/bin/bash.exe
        if candidate.is_file():
            return str(candidate)
    return None


def gateway(argv: list[str]) -> int:
    sub = argv[0] if argv else "up"
    rest = argv[1:]
    from mme.gateway import render
    if sub == "render":
        return render.main(rest)
    env = _load_dotenv(os.environ)
    python = Path(sys.executable).as_posix()  # bash-safe on Windows (no backslashes)
    env.update(MME_PKG=str(PACKAGE_DIR), MME_BIN=f"{python} -m mme", MME_PYTHON=python,
               MME_GATEWAY_CONFIG=str(render.config_path()), MME_STATE=str(state_dir()), MME_PROJECT=str(project_root()))
    if sub == "down":
        return subprocess.run(["docker", "compose", "--profile", "mock", "down"], cwd=PACKAGE_DIR / "gateway", env=env).returncode
    scripts = {"up": "up.sh", "check": "check.sh", "drill": "drill.sh", "backup": "backup.sh"}
    if sub not in scripts:
        print(f"usage: mme gateway <{'|'.join([*scripts, 'render', 'down'])}>", file=sys.stderr)
        return 2
    bash = find_bash()
    if not bash:
        print("mme gateway needs bash (on Windows: Git for Windows, https://git-scm.com)", file=sys.stderr)
        return 127
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})  # so `render` inside sees .env too
    return subprocess.run([bash, str(PACKAGE_DIR / "gateway" / scripts[sub]), *rest], env=env).returncode


def doctor() -> int:
    ok = True

    def line(good: bool, msg: str, fix: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  {'✅' if good else '❌'} {msg}" + (f"\n       -> {fix}" if fix and not good else ""))

    print(f"MME {__version__} · project {project_root()}")
    print("Tools")
    for tool, why in (("git", "worktrees"), ("claude", "harness"), ("docker", "gateway"), ("rg", "context protocol")):
        line(shutil.which(tool) is not None, f"{tool} ({why})", f"install {tool}")
    try:
        import yaml  # noqa: F401
        line(True, "pyyaml")
    except ImportError:
        line(False, "pyyaml", "pip install pyyaml")
    line(shutil.which("mme") is not None, "`mme` on PATH (hooks call it)", "pip install -e <mme repo> in the env you start claude from")
    print("Config")
    cd = config_dir()
    for f in ("budget.json", "policy.env", "milestones.yaml", "night/missions.yaml"):
        line((cd / f).exists(), f"mme/{f}", "mme init")
    try:
        from mme import budget
        probs = budget.validate(budget.load_plan())
        line(not probs, "budget plan valid", "; ".join(probs))
    except Exception as e:  # noqa: BLE001
        line(False, f"budget plan readable ({e})")
    try:
        from mme.night import missions
        n = len(missions.load_missions())
        line(True, f"missions.yaml parses ({n} missions)")
    except Exception as e:  # noqa: BLE001
        line(False, f"missions.yaml parses ({e})")
    try:
        from mme import gates
        gates.load()
        line(True, "milestones.yaml parses")
    except Exception as e:  # noqa: BLE001
        line(False, f"milestones.yaml parses ({e})")
    s = project_root() / ".claude" / "settings.json"
    line(s.exists() and "mme hook loop-breaker" in s.read_text(), ".claude/settings.json wires the MME hooks", "mme init --force")
    a = project_root() / "AGENTS.md"
    line(a.exists() and "mme:start" in a.read_text(), "AGENTS.md has the MME block", "mme init")
    print("Secrets (this shell)")
    env = _load_dotenv(os.environ)
    for k, why in (("MODEL_API_KEY", "product live mode, smoke"), ("LITELLM_MASTER_KEY", "gateway admin"),
                   ("POSTGRES_PASSWORD", "gateway DB"), ("MME_GATEWAY_KEY", "day harness"), ("MME_NIGHT_KEY", "night")):
        line(bool(env.get(k)), f"{k} ({why})", "add to .env (see .env.example)")
    gi = project_root() / ".gitignore"
    line(gi.exists() and ".env" in gi.read_text(), ".env is gitignored", "mme init")
    print("\n" + ("All good." if ok else "Fix the ❌ items above (some are expected before kickoff)."))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if cmd in ("--version", "version"):
        print(__version__)
        return 0
    if cmd == "shell-init":
        from mme.harness import SHELL_INIT
        print(SHELL_INIT)
        return 0
    if cmd in ("status", "use", "fallback", "policy"):
        from mme import harness
        return {"status": lambda: harness.cmd_status(), "use": lambda: harness.cmd_use(rest),
                "fallback": lambda: harness.cmd_fallback(rest), "policy": lambda: harness.cmd_policy(rest)}[cmd]()
    if cmd == "init":
        from mme import init
        return init.main(rest)
    if cmd == "doctor":
        return doctor()
    if cmd == "budget":
        from mme import budget
        return budget.main(rest)
    if cmd == "credits":
        from mme import credits
        return credits.main(rest)
    if cmd == "watchdog":
        from mme import watchdog
        return watchdog.main(rest)
    if cmd == "smoke":
        from mme import smoke
        return smoke.main(rest)
    if cmd == "mock":
        from mme.mock import meta_mock
        return meta_mock.main(rest)
    if cmd == "models":
        from mme import providers
        print(providers.markdown_table())
        return 0
    if cmd == "night":
        from mme.night import missions
        if rest[:1] == ["run"] and "--dry-run" not in rest:
            inner = [sys.executable, "-m", "mme", "_night", *rest]
            if shutil.which("caffeinate"):
                return subprocess.run(["caffeinate", "-i", *inner]).returncode
            if shutil.which("systemd-inhibit"):
                return subprocess.run(["systemd-inhibit", "--what=idle:sleep", "--why=mme night run", *inner]).returncode
            print("[mme] can't prevent sleep here: disable sleep in your power settings tonight", file=sys.stderr)
        return missions.main(rest)
    if cmd == "_night":
        from mme.night import missions
        return missions.main(rest)
    if cmd in ("today", "gate", "ship"):
        from mme import gates
        return gates.main(rest, cmd)
    if cmd == "gateway":
        return gateway(rest)
    if cmd == "hook":
        if rest[:1] == ["session-start"]:
            from mme.hooks import session_start
            return session_start.main()
        if rest[:1] == ["loop-breaker"]:
            from mme.hooks import loop_breaker
            return loop_breaker.main()
        if rest[:1] == ["announce-guard"]:
            from mme.hooks import announce_guard
            return announce_guard.main()
        print("usage: mme hook <session-start|loop-breaker|announce-guard>", file=sys.stderr)
        return 2
    print(f"unknown command '{cmd}'\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
