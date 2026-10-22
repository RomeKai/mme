"""Circuit breaker for agent retry loops (fail -> edit -> fail -> edit ...).

Wired in .claude/settings.json as `mme hook loop-breaker` on:
  PostToolUseFailure (Bash)  -> count a failure for this command signature
  PostToolUse        (Bash)  -> success resets that signature
  UserPromptSubmit           -> the human stepped in: reset everything

Thresholds (env): MME_LOOP_WARN=2, MME_LOOP_STOP=3 consecutive failures of the same command,
MME_LOOP_SESSION_STOP=8 failures of any command since the human last spoke.
At STOP the hook returns {"continue": false}: Claude halts and hands control back to you.
State lives in .mme/loops/<session_id>.json. Never crashes the session: any internal error -> exit 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WARN = int(os.environ.get("MME_LOOP_WARN", "2"))
STOP = int(os.environ.get("MME_LOOP_STOP", "3"))
SESSION_STOP = int(os.environ.get("MME_LOOP_SESSION_STOP", "8"))
# Shell tool name differs per harness (Claude Code: Bash). Muse Code: set MME_SHELL_TOOLS after checking its docs.
SHELL_TOOLS = set(os.environ.get("MME_SHELL_TOOLS", "Bash").split(","))

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_WRAPPERS = {"cd", "uv", "poetry", "npx", "pnpm", "yarn", "bunx", "python", "python3", "-m", "run", "exec"}


def signature(command: str) -> str:
    """'cd server && MME_MODE=replay python -m pytest -q tests/x.py' -> 'pytest'.

    Uses the last chained command, strips env assignments and wrappers, keeps the tool name
    (+ the npm script name), so flag/path tweaks between attempts still count as the same loop.
    """
    last = re.split(r"&&|\|\||;", command)[-1]      # last chained command...
    last = last.split("|")[0].strip()                 # ...without its output pipe (| tail, | grep)
    toks = [t for t in last.split() if not _ENV_ASSIGN.match(t)]
    toks = [t for t in toks if t not in _WRAPPERS] or toks
    if not toks:
        return command.strip()[:40]
    head = os.path.basename(toks[0])
    if head == "npm" and len(toks) > 1:
        return f"npm {toks[1]}"
    return head


def state_path(cwd: str, session_id: str) -> Path:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", cwd))
    return root / ".mme" / "loops" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', session_id)}.json"


def handle(event: dict) -> dict | None:
    """Returns the JSON to print (or None). Pure enough to unit-test."""
    name = event.get("hook_event_name", "")
    path = state_path(event.get("cwd", "."), event.get("session_id", "unknown"))
    state = json.loads(path.read_text()) if path.exists() else {"by_sig": {}, "total": 0}

    if name == "UserPromptSubmit":
        state = {"by_sig": {}, "total": 0}
        out = None
    elif event.get("tool_name") not in SHELL_TOOLS:
        return None
    else:
        sig = signature((event.get("tool_input") or {}).get("command", ""))
        if name == "PostToolUse":
            state["by_sig"].pop(sig, None)
            out = None
        elif name == "PostToolUseFailure":
            n = state["by_sig"].get(sig, 0) + 1
            state["by_sig"][sig] = n
            state["total"] += 1
            if n >= STOP or state["total"] >= SESSION_STOP:
                out = {
                    "continue": False,
                    "stopReason": (
                        f"[MME loop-breaker] '{sig}' failed {n}x in a row ({state['total']} failures since your last "
                        "message). Agent stopped to protect credits. Read the last error, then tell it what to try "
                        "(or raise MME_LOOP_STOP)."
                    ),
                }
            elif n >= WARN:
                out = {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUseFailure",
                        "additionalContext": (
                            f"[MME loop-breaker] '{sig}' has failed {n}x. Do NOT retry with another small tweak. "
                            "Re-read the full error, state one hypothesis about the root cause, and check it with a "
                            f"targeted read. One more failure ({STOP}) stops the session."
                        ),
                    }
                }
            else:
                out = None
        else:
            return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        out = handle(json.load(sys.stdin))
        if out:
            print(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - a broken guard must never break the session
        print(f"[MME loop-breaker] internal error ignored: {e}", file=sys.stderr)
    return 0
