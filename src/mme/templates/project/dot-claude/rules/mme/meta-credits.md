# MME rule: provider credits are a finite build resource (plan: mme/budget.json)

Always-loaded. Applies to product code and to how you (the agent) work.

## Product code
- All Meta calls go through `mme.client` (`from mme.client import client`: `chat`, `chat_json`, `image_generate`). Never call `api.meta.ai` from anywhere else.
- Every call passes a `tag` (e.g. `brief->program`, `render`, `eval`) so `mme credits` can attribute spend.
- Default tier is `draft` (Spark contributor). Use `standard` only for evals and recording demo outputs. `max` (Wonder) needs a written reason in the PR/commit.
- Tests and CI run with `MME_MODE=replay`. A test that needs the network is a bug.
- Prompt layout for cache hits: stable system prompt + schema + few-shot FIRST, variable brief LAST. No timestamps/IDs in the prefix. Pass a stable `prompt_cache_key` per pipeline.
- Generate images only from the UI's explicit "Render" action or the eval script; never in loops, never on page load.
- Before adding a new paid call, estimate its cost per demo run and write it in the PR description.

## Agent behavior (always, and strictly when the harness runs on Meta)
- **Retry discipline:** after 2 failures of the same command, stop tweaking. Re-read the full error, write one root-cause
  hypothesis, verify it with a targeted read. The loop-breaker hook stops the session at 3.
- **No unattended loops:** do not use ECC `loop-operator`, `autonomous-loops`, `/multi-*` or `claude -p` without
  `--max-turns 15`, unless the human asks for it explicitly in this session.
- **Surgical context:** locate with `Grep`/`Glob` first, then `Read` with `offset`/`limit` on the 2-3 files that matter.
  Use the repo map in AGENTS.md. Never read lockfiles, generated files, `samples/replay/`, `.cache/`, DXF files.
- **Subagents:** at most 2 in parallel, each with a narrow task and an explicit list of files to read. The gateway
  caps parallel requests at 4.
- **Never** touch MME controls: `mme policy|use|fallback|gateway|budget sync|watchdog --unblock`, or gateway `/key/*`
  endpoints. Budget and harness are the human's decisions (they're also blocked in settings.json).
- **Heavy model:** only the human switches to `muse-wonder-1.0` (`/model`). Suggest it when Spark has failed twice on
  the same design problem; never for planning, summaries, docs or simple edits.
- Use `/context-budget` when context grows; prefer `/save-session` + a fresh session over a huge context.
