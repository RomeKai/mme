"""`mme init`: install MME's project files into the current repo (idempotent, never clobbers your edits).

Creates (skips if present unless --force):
  mme/budget.json  mme/policy.env  mme/milestones.yaml  mme/night/missions.yaml  mme/harness/*
  .claude/settings.json  .claude/rules/mme/meta-credits.md  .claude/skills/meta-model-api/  .claude/agents/meta-integrator.md
  .env.example (only if missing)
Merges (managed block between markers, always refreshed):
  AGENTS.md (MME rules block)   .gitignore (MME entries)   CLAUDE.md (created with `@AGENTS.md` if missing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

from mme import providers
from mme.paths import PACKAGE_DIR, project_root

TEMPLATES = PACKAGE_DIR / "templates"
GITIGNORE_BLOCK = """# mme:start
.env
.env.*
!.env.example
.mme/
HANDOFF.md
NIGHT_REPORT.md
.mission/
# mme:end
"""


def _target(rel: Path) -> Path:
    parts = [p.replace("dot-", ".", 1) if p.startswith("dot-") else p for p in rel.parts]
    return Path(*parts)


def _render(text: str, start: dt.date) -> str:
    end = start + dt.timedelta(days=9)
    return (text.replace("{{START}}", start.isoformat()).replace("{{END}}", end.isoformat())
            .replace("{{FREEZE}}", (end - dt.timedelta(days=2)).isoformat())
            .replace("{{MODEL_TABLE}}", providers.markdown_table()))


def merge_block(path: Path, block: str, start_marker: str, end_marker: str) -> str:
    text = path.read_text() if path.exists() else ""
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker) + r"\n?", re.S)
    if pattern.search(text):
        new = pattern.sub(block, text)
        status = "updated block" if new != text else "unchanged"
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block
        status = "added block" if text else "created"
    path.write_text(new)
    return status


def init(root: Path, force: bool = False, start: dt.date | None = None) -> list[tuple[str, str]]:
    start = start or dt.date.today()
    report = []
    src_root = TEMPLATES / "project"
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = _target(src.relative_to(src_root))
        dst = root / rel
        existed = dst.exists()
        if existed and not force:
            report.append((str(rel), "skipped (exists)"))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(_render(src.read_text(), start))
        report.append((str(rel), "overwritten" if existed else "created"))
    block = (TEMPLATES / "AGENTS.block.md").read_text()
    report.append(("AGENTS.md", merge_block(root / "AGENTS.md", block, "<!-- mme:start", "<!-- mme:end -->")))
    report.append((".gitignore", merge_block(root / ".gitignore", GITIGNORE_BLOCK, "# mme:start", "# mme:end")))
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text("@AGENTS.md\n")
        report.append(("CLAUDE.md", "created (@AGENTS.md)"))
    elif "@AGENTS.md" not in claude_md.read_text():
        report.append(("CLAUDE.md", "exists without @AGENTS.md: add that line so Claude Code reads the MME rules"))
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mme init")
    ap.add_argument("--force", action="store_true", help="overwrite template files (not your AGENTS.md text)")
    ap.add_argument("--start", help="first day of the budget plan / milestones (YYYY-MM-DD, default today)")
    a = ap.parse_args(argv)
    root = project_root()
    for path, status in init(root, a.force, dt.date.fromisoformat(a.start) if a.start else None):
        print(f"  {status:<22} {path}")
    print(f"\nMME initialised in {root}. Next: edit mme/budget.json + mme/milestones.yaml, then `mme doctor`.")
    return 0
