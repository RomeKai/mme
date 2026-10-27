"""Pick the coding harness under a rules policy, with fallback, handoff and an AI-usage log.

    mme status | mme use <harness> [reason] | mme fallback [reason] | mme policy <policy>

Policies (<project>/mme/policy.env, committed):
    meta-only   the AI used to BUILD must be Meta -> Claude is refused
    meta-first  Muse primary, Claude allowed as a session-level fallback (default)
    open        anything goes

`use`/`fallback` print shell code on stdout (the shell function from `mme shell-init` evals it); messages go to
stderr. Secrets are referenced as $VARS and never printed. The agent's shell is stripped of admin secrets
(LiteLLM master key, night key, and the raw Meta key when the harness doesn't need it), so the agent can't raise
its own budget or unblock keys.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

from mme import providers
from mme.paths import config_dir, project_root

POLICIES = ("meta-only", "meta-first", "open")
GATEWAY_URL = os.environ.get("MME_GATEWAY_URL", "http://127.0.0.1:4000")

CLAUDE_VARS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL", "ENABLE_TOOL_SEARCH", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "META_API_KEY",
)
ADMIN_SECRETS = ("LITELLM_MASTER_KEY", "MME_NIGHT_KEY", "POSTGRES_PASSWORD")


def claude_code_env(base_url: str, token_var: str, model: str | None = None) -> dict[str, str]:
    """Claude Code pointed at a non-Anthropic backend. EVERY alias is mapped (ECC agents ask for `opus`)."""
    m = model or providers.harness_model("cheap")
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": f"${token_var}" if token_var else "",
        "ANTHROPIC_MODEL": m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m,
        "ANTHROPIC_SMALL_FAST_MODEL": m,
        "CLAUDE_CODE_SUBAGENT_MODEL": m,
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
    }


HARNESSES: dict[str, dict] = {
    "gateway": {
        "provider": "meta", "needs": ["MME_GATEWAY_KEY"], "keep": [],
        "env": lambda: claude_code_env(GATEWAY_URL, "MME_GATEWAY_KEY"),
        "start": f"claude      (heavy problem: /model {providers.harness_model('heavy')}, then back)",
    },
    "meta-direct": {
        "provider": "meta", "needs": ["MODEL_API_KEY"], "keep": ["MODEL_API_KEY"],
        "env": lambda: claude_code_env(providers.load()["anthropic_base_url"], "MODEL_API_KEY"),
        "start": "claude      (no daily cap: watch the console)",
    },
    "muse-code": {
        "provider": "meta", "needs": ["MODEL_API_KEY"], "keep": ["MODEL_API_KEY"],
        "env": lambda: {"META_API_KEY": "$MODEL_API_KEY"},
        "start": "muse        (install: curl -fsSL https://dev.meta.ai/install.sh | bash)",
    },
    "opencode": {
        "provider": "meta", "needs": ["MODEL_API_KEY"], "keep": ["MODEL_API_KEY"],
        "env": lambda: {},
        "start": "opencode    (config: see `mme init` templates; store key with /connect)",
    },
    "claude": {
        "provider": "anthropic", "needs": [], "keep": [],
        "env": lambda: {},
        "start": "claude      (Anthropic plan: spends no Meta credits)",
    },
}

CHAIN = {
    "meta-only": ["gateway", "meta-direct", "muse-code", "opencode"],
    "meta-first": ["gateway", "meta-direct", "muse-code", "claude"],
    "open": ["gateway", "meta-direct", "muse-code", "claude", "opencode"],
}


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def policy_file():
    return config_dir() / "policy.env"


def usage_log():
    return project_root() / "docs" / "AI_USAGE.md"


def handoff_file():
    return project_root() / "HANDOFF.md"


def read_policy() -> str:
    if os.environ.get("MME_POLICY"):
        return os.environ["MME_POLICY"]
    pf = policy_file()
    if pf.exists():
        for line in pf.read_text().splitlines():
            if line.startswith("MME_POLICY="):
                return line.split("=", 1)[1].strip()
    return "meta-first"


def allowed(policy: str, harness: str) -> bool:
    return harness in CHAIN[policy]


def exports(harness: str) -> str:
    h = HARNESSES[harness]
    strip = [*CLAUDE_VARS, *ADMIN_SECRETS, *(["MODEL_API_KEY"] if "MODEL_API_KEY" not in h["keep"] else [])]
    lines = [f"unset {' '.join(strip)}"]
    for k, v in h["env"]().items():
        lines.append(f'export {k}="{v}"')  # $VAR refs expand only at eval time -> never printed
    lines.append(f'export MME_HARNESS="{harness}"')
    return "\n".join(lines)


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=project_root(), capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def write_handoff(src: str, dst: str, reason: str) -> None:
    handoff_file().write_text(
        f"# Handoff {src} -> {dst}\n\n"
        f"- When: {dt.datetime.now():%Y-%m-%d %H:%M}\n- Why: {reason or 'manual switch'}\n\n"
        "## Fill before switching (30 seconds)\n- Was doing:\n- Next step:\n- Failing test / error:\n\n"
        f"## Git status\n```\n{git('status', '--short') or '(clean or no git)'}\n```\n\n"
        f"## Last commits\n```\n{git('log', '-5', '--oneline') or '(none)'}\n```\n"
        "\nThe new session reads this first (AGENTS.md says so), then deletes it once it has taken over.\n"
    )


def log_usage(harness: str, policy: str, reason: str, kind: str = "interactive") -> None:
    log = usage_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text(
            "# AI usage log\n\nWritten by MME on every harness switch and night run. "
            "Evidence for the submission's AI disclosure.\n\n"
            "| When | Kind | Harness | Provider | Model | Policy | Reason |\n|---|---|---|---|---|---|---|\n"
        )
    h = HARNESSES.get(harness, {"provider": "meta", "env": lambda: {}})
    model = h["env"]().get("ANTHROPIC_MODEL", "harness default") if h["provider"] == "meta" else "Claude (Anthropic plan)"
    with log.open("a") as f:
        f.write(f"| {dt.datetime.now():%Y-%m-%d %H:%M} | {kind} | {harness} | {h['provider']} | {model} | {policy} | "
                f"{reason or '-'} |\n")


def switch(target: str, policy: str, reason: str) -> int:
    if target not in HARNESSES:
        err(f"unknown harness '{target}'. Options: {', '.join(HARNESSES)}")
        return 2
    if not allowed(policy, target):
        err(f"[mme] '{target}' is NOT allowed under policy '{policy}'. Allowed: {', '.join(CHAIN[policy])}")
        return 3
    missing = [v for v in HARNESSES[target]["needs"] if not os.environ.get(v)]
    if missing:
        err(f"[mme] '{target}' needs {', '.join(missing)} (set -a; source .env; set +a)")
        return 4
    current = os.environ.get("MME_HARNESS")
    if current and current != target:
        write_handoff(current, target, reason)
        err("[mme] wrote HANDOFF.md: fill the 3 lines, and in the old session run /save-session if it's still alive.")
    log_usage(target, policy, reason)
    if target == "gateway" and os.environ.get("LITELLM_MASTER_KEY"):
        try:  # move today's cap before the master key is stripped from this shell; never block the switch
            from mme import budget
            budget.main(["sync", "--quiet"])
        except Exception as e:  # noqa: BLE001
            err(f"[mme] budget sync skipped: {e}")
    print(exports(target))
    err(f"[mme] harness -> {target} ({HARNESSES[target]['provider']}), policy {policy}. Start: {HARNESSES[target]['start']}")
    err("[mme] admin secrets removed from THIS shell (agent terminal). Keep a second terminal for mme ops.")
    return 0


def cmd_status() -> int:
    policy = read_policy()
    err(f"policy: {policy}\ncurrent harness: {os.environ.get('MME_HARNESS', '(none)')}\n"
        f"chain: {' -> '.join(CHAIN[policy])}\nproject: {project_root()}")
    return 0


def cmd_use(args: list[str]) -> int:
    if not args:
        err(f"usage: mme use <{'|'.join(HARNESSES)}> [reason]")
        return 2
    return switch(args[0], read_policy(), " ".join(args[1:]))


def cmd_fallback(args: list[str]) -> int:
    policy = read_policy()
    chain = CHAIN[policy]
    cur = os.environ.get("MME_HARNESS")
    nxt = chain[(chain.index(cur) + 1) % len(chain)] if cur in chain else chain[0]
    if nxt == cur:
        err("[mme] no other harness available under this policy")
        return 5
    return switch(nxt, policy, " ".join(args) or "fallback")


def cmd_policy(args: list[str]) -> int:
    if not args or args[0] not in POLICIES:
        err(f"usage: mme policy <{'|'.join(POLICIES)}>")
        return 2
    pf = policy_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(f"# Set from the official rules. Changed {dt.date.today()}.\nMME_POLICY={args[0]}\n")
    err(f"[mme] policy -> {args[0]} (commit mme/policy.env)")
    cur = os.environ.get("MME_HARNESS")
    if cur and not allowed(args[0], cur):
        err(f"[mme] current harness '{cur}' is no longer allowed: run `mme use gateway`")
    return 0


SHELL_INIT = r'''# MME shell integration: eval "$(mme shell-init)"
mme() {
  case "${1:-}" in
    use|fallback)
      local out
      out="$(command mme "$@")" || return $?
      eval "$out"
      ;;
    *) command mme "$@" ;;
  esac
}
'''
