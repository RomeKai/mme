# Roadmap and audit

## Audit of the pre-0.2 kit (2026-10-22) → what changed

| # | Finding | Risk | Fix in 0.2 |
|---|---|---|---|
| 1 | MME spread over 6 folders of the product repo | Not reusable; tooling mixed with product code | Standalone package + `mme init` |
| 2 | Model IDs in 13 files, prices in 4 | A price change means silent wrong budgets | `providers/meta.json` is the single source. The gateway config is *generated* |
| 3 | Agent shell held `LITELLM_MASTER_KEY` | The agent could raise its own budget or unblock keys | `mme use` strips admin secrets. Permissions deny `/key/*` and MME controls |
| 4 | Product code (client, DXF seed) written before the event | Rule violation risk | The client is now open-source tooling (`mme.client`). The product repo starts empty |
| 5 | Plan was prose only | Nobody notices being behind until it's too late | Milestones with executable gates, `mme today`, `mme ship` |
| 6 | Harness spend logged by hand | The ledger was always wrong | `mme credits` reads both gateway keys |
| 7 | Night tasks were single-shot and narrow | Couldn't hand off real work | Missions: briefing, stages, repair, cheap read-only review, scope check |
| 8 | Fixed $6/day | Didn't match real weeks | Variable plan + rollover + daily ceiling + prelaunch |
| 9 | Skills copied into two dirs | Drift | One template source, installed by `mme init` |
| 10 | No packaging, CI or license | Not shareable | pyproject, MIT, CI matrix, CHANGELOG |

## 0.3 (after the hackathon: harden with real-world data)
- Replace every "unverified" note with a test against the real API (recorded fixtures for the mock).
- Muse Code adapter: generate `.muse/hooks.json` once the schema is documented, and use `PreLLMCall` as a budget gate.
- `mme.client`: Responses API (reasoning replay), SAM 3.1 and Voice Transcribe methods, streaming.
- `mme credits --html`: daily spend/burn-down page; `mme today --json` for status bars.
- Mission templates from real usage: `refactor`, `docs`, `eval` (prompt regression with replay fixtures).

## 0.4
- Provider plugins: any OpenAI/Anthropic-compatible provider via `providers/<name>.json` (not just Meta).
- Native Windows (PowerShell gateway scripts).
- PyPI release, docs site, example projects.

## 1.0 criteria
- Used end-to-end on at least 3 real projects; zero unverified integration notes.
- Upgrade path that never touches user-edited files (already true for `mme init`).
- Security review of hooks/permissions (AgentShield scan in CI).
