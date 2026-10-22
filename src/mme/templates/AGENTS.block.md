<!-- mme:start (managed by `mme init`; edit outside these markers) -->
## MME harness rules (all agents: Claude Code, Muse Code, OpenCode, Codex)

**First:** if `HANDOFF.md` exists at the repo root, another harness was working before you: read it, confirm the next
step with the human, delete it once you've taken over. If `NIGHT_REPORT.md` exists and the human asks about last
night, start there.

**Credits are finite.** Rule: `.claude/rules/mme/meta-credits.md`. Product code calls the provider ONLY through
`mme.client`. Run product commands with `MME_MODE=replay` unless the human says otherwise (spends $0).

**Never touch MME controls:** harness/policy (`mme use|fallback|policy`), budgets (`mme budget sync`, `mme gateway`,
gateway `/key/*`), the watchdog. Those are the human's decisions.

**Context protocol (every token costs):**
- Never read `.venv/`, `site-packages/`, `node_modules/`, lockfiles, `dist/`, `.cache/`, `.mme/`, `samples/replay/`,
  binaries or generated/minified files.
- Find before you read: repo map -> `rg -n --max-count 5 "<symbol>"` -> `rg -l "<term>" | head -20` ->
  `rg --files -g '*<part>*'`. Then read only the 2-3 files that matter; files > 300 lines by range (±60 lines).
- Third-party libraries: never open their source. Use `python -c "import inspect, pkg; print(inspect.signature(...))"`,
  `help(...) | head -40`, or fetch the specific docs page.
- Bound every output: `pytest -q 2>&1 | tail -40`, `npm run build 2>&1 | tail -30`. No `cat` of whole files, no `ls -R`.
- Don't re-read a file that's already in context.

**Retry discipline:** after 2 failures of the same command, stop tweaking: re-read the full error, state one root-cause
hypothesis, verify it with a targeted read. The loop-breaker hook stops the session at 3.

**Status:** `mme today` shows the deadline, today's goal and what's behind. Suggest `mme gate` when you believe the
day's goal is done; it is the definition of done, not your judgment.
<!-- mme:end -->
