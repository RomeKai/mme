"""SessionStart hook (`mme hook session-start`): a few lines of situational awareness for the agent.

Never fails the session; never touches the network except the local gateway (and only if keys are present).
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout


def lines() -> list[str]:
    from mme import credits, harness
    out = []
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            credits.main(["--short"])
        out.append(buf.getvalue().strip())
    except Exception:  # noqa: BLE001
        pass
    out.append(f"[MME] policy: {harness.read_policy()} · harness: {os.environ.get('MME_HARNESS', 'unset (run: mme use gateway)')}")
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    model = os.environ.get("ANTHROPIC_MODEL", "?")
    if "127.0.0.1:4000" in base or "localhost:4000" in base:
        out.append(f"[MME] backend: gateway -> {model}, daily cap active. Be concise; targeted reads; after 2 failures "
                   "of the same command stop and form a hypothesis.")
    elif "api.meta.ai" in base:
        out.append(f"[MME] backend: Meta API direct ({model}), NO daily cap. Every token spends credits.")
    else:
        out.append("[MME] backend: Anthropic (no Meta credits used).")
    try:
        from mme import gates
        s = gates.short_line(gates.load())
        if s:
            out.append(s)
    except Exception:  # noqa: BLE001
        pass
    from mme.paths import project_root
    if (project_root() / "HANDOFF.md").exists():
        out.append("[MME] HANDOFF.md exists: read it first, confirm the next step with the human, then delete it.")
    if (project_root() / "NIGHT_REPORT.md").exists() and os.environ.get("MME_HARNESS", "").startswith("night") is False:
        out.append("[MME] NIGHT_REPORT.md exists: if the human asks about last night, start there.")
    return [l for l in out if l]


def main(argv: list[str] | None = None) -> int:
    try:
        print("\n".join(lines()))
    except Exception as e:  # noqa: BLE001
        print(f"[MME] session-start hook error ignored: {e}")
    return 0
