"""Stop hook: a reasoning model sometimes announces an action ("Running the tests now.") and ends its turn
without the tool call, so Claude Code thinks it's done. Nudge it once to act.

Wired as `mme hook announce-guard` on Stop. Uses `stop_hook_active` so it fires at most once per stop: never
a loop. A question to the human or a final report passes through. Never crashes the session.
"""
from __future__ import annotations

import json
import re
import sys

_INTENT = re.compile(
    r"^(now|next|then|let me|let's|i'll|i will|i'm going to|going to|continuing|retrying|rewriting|running|"
    r"checking|verifying|updating|fixing|applying|writing|adding|starting|moving on|proceeding)\b",
    re.IGNORECASE,
)
REASON = ("You announced an action but ended your turn without a tool call. Do it now. If the work is truly "
          "finished, reply with a short final report (commit SHAs, test results) instead.")


def _last_sentence(text: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if p.strip()]
    return parts[-1] if parts else ""


def announced_without_acting(text: str) -> bool:
    text = (text or "").strip()
    if not text or text.endswith("?"):
        return False
    if text.endswith(":"):
        return True
    last = _last_sentence(text).lstrip("-*• ").strip()
    return bool(_INTENT.match(last)) or bool(re.search(r"\b(now|next)\.?$", last, re.IGNORECASE))


def decide(payload: dict) -> dict | None:
    if payload.get("stop_hook_active"):
        return None
    if announced_without_acting(payload.get("last_assistant_message") or ""):
        return {"decision": "block", "reason": REASON}
    return None


def main() -> int:
    try:
        out = decide(json.load(sys.stdin))
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0
