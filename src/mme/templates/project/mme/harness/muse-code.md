# Muse Code as a harness

Muse Code is Meta's own agent CLI. It's the safest harness if the rules say "build with Meta tools".

**Install:** `curl -fsSL https://dev.meta.ai/install.sh | bash`, then `mme use muse-code` and `muse`.
It uses `META_API_KEY`, which `mme use muse-code` points at your `MODEL_API_KEY`. Trust the workspace when it asks; without that it ignores
the project's rules, skills and hooks.

## What carries over from this repo (per Meta's docs)
| Piece | Claude Code | Muse Code |
|---|---|---|
| Project instructions | `CLAUDE.md` -> `@AGENTS.md` | `AGENTS.md` (read first) |
| Project skills (`.claude/skills/*/SKILL.md`) | yes | yes (scans `.claude/skills` and `.agents/skills`) |
| ECC skills | via plugin | copy the subset into `.agents/skills/` (`setup.sh` does it) or `muse skills import --from claude` |
| ECC agents (`agents/*.md`) | yes | no file format: Muse spawns subagents itself (`agents.execution_capacity`, default 8 -> set 3) |
| Hooks | `.claude/settings.json` | `.muse/hooks.json`, same event names, **schema not documented** |
| Daily $ cap | LiteLLM gateway | not documented -> watch the console; `muse exec --max-model-steps N` in headless |

## Day-0 tasks (15 min, after reading dev.meta.ai/docs/muse-code/extending)
1. Write `.muse/hooks.json` binding `PostToolUseFailure`, `PostToolUse` and `UserPromptSubmit` to
   `mme hook loop-breaker`. If Muse's shell tool isn't called `Bash`, set `MME_SHELL_TOOLS`
   (e.g. `export MME_SHELL_TOOLS=Bash,shell`).
2. Look at the `PreLLMCall` event: if its hook can block the call, wire budget checks (`mme budget`) there.
   That gives Muse Code the same hard cap the gateway gives Claude Code.
3. In `~/.config/muse/settings.json` (it needs `"schema_version": 1`): `agents.execution_capacity: 3`.
4. `muse skills list` should show `meta-model-api`, `dxf-export`, etc.

Until 1-3 are done, Muse Code runs **without** the loop breaker and budget gate. Use it for scoped tasks only.
