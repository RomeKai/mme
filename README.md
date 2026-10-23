# MME — budget-safe agent harness for the Meta Model API

**MME** sits on top of [ECC](https://github.com/affaan-m/ECC) (Everything Claude Code). It lets you run Claude Code,
Muse Code or OpenCode on Meta's **Muse** models without burning your credits, and without missing your deadline.

ECC gives your agent a way of working: plan → test → implement → review. MME adds what that way of working needs once
**credits are finite and the date is fixed**:

| Problem | MME |
|---|---|
| "Which agent/model am I allowed to use?" | `mme use <harness>` under a **rules policy** (`meta-only` / `meta-first` / `open`), with fallback chain, handoff file and an **AI-usage log** for disclosure |
| A retry loop drains $40 overnight | **Gateway budgets** (LiteLLM + Postgres): variable daily plan with rollover, separate night pool. **Loop breaker** hook (stops after 3 identical failures). **Burst watchdog** (blocks the key on spend spikes) |
| The agent raises its own budget | Agent shells are **stripped of admin secrets**. `settings.json` denies MME controls and gateway `/key/*` |
| You want work done while you sleep | **Night missions**: a pre-bed **briefing** where the agent asks its questions, then staged runs (plan → build → harden → cheap review) on their own branches, verified by the runner, with a morning report |
| "Are we on track?" | **Milestones with executable gates**, `mme today` countdown + "behind / cut this", `mme ship` submission checklist |
| Product calls cost money in tests | `mme.client`: tiers, disk cache, **replay mode** ($0 tests and demos), ledger, hard stop |
| Prices and model IDs drift | **One provider catalog** (`providers/meta.json`). It feeds cost math, the gateway config, harness mapping, the mock and the docs |
| You can't test any of this without a key | A **local mock** of the API plus a gateway **fire drill** (`mme gateway drill`), the whole gateway against the mock (`mme gateway up --mock`), and night missions rehearsable on `harness: claude` |

> Status: **0.2, pre-release.** Built for the Meta Global AI Developer Hackathon (Oct–Nov 2026). Endpoints and
> prices were checked against dev.meta.ai on 2026-10-22. The [unverified items](#known-gaps) are listed, not hidden.

## Install

```bash
pip install -e path/to/mme          # or: pipx install git+https://github.com/<you>/mme
cd your-project && git init         # MME works per git repo
mme init --start 2026-10-26         # writes mme/, .claude/, AGENTS.md block, .gitignore block
eval "$(mme shell-init)"            # add to ~/.zshrc / ~/.bashrc: enables `mme use` / `mme fallback`
mme doctor
```
Install ECC itself once from its official source (`npx ecc-universal@2.2.3 setup`). MME never modifies ECC.

## Two terminals

| Agent terminal | Ops terminal |
|---|---|
| `mme use gateway && claude` | `set -a; source .env; set +a` |
| No admin secrets in this shell (stripped by `mme use`) | `mme watchdog` · `mme today` · `mme gate` · `mme night prep/run` · `mme credits` |

## Day loop
```bash
mme today                      # countdown, today's goal, what's behind and what to cut
mme use gateway && claude      # /ecc:plan ... -> build -> /code-review
mme gate                       # the definition of done is the gate, not the agent's opinion
mme night prep                 # before bed: pre-flight + the agent's questions (2-5 min)
mme night run --until 07:30
# morning: NIGHT_REPORT.md -> review branches -> merge what's good
```

## Night missions
```yaml
# mme/night/missions.yaml
missions:
  - id: layout-engine-v1
    template: feature            # plan 10% -> build 60% -> harden 20% -> review (≤ $0.30)
    budget_usd: 3.0
    goal: |
      Deterministic layout engine ...
    context: [AGENTS.md, server/schemas.py]
    scope: ["server/layout/**", "server/tests/test_layout*.py"]
    verify: "cd server && MME_MODE=replay python -m pytest -q tests/test_layout.py"
```
`mme night prep` checks git, context, budgets and the gateway. Then it asks the agent, read-only and with your day key,
what it needs to know. You answer or accept its defaults. The answers are pinned to the mission's content hash:
if you edit the mission, you need to run prep again.

`mme night run` executes each mission:
- in a **worktree + branch**, starting from `base`;
- stage by stage, with a shared `.mission/PROGRESS.md`;
- with **one repair attempt** when a check fails;
- with per-stage and per-mission spend kills, and a reservation-based per-night cap.

The review stage is **read-only and cheap**: edits outside `.mission/` are reverted. Files changed out of `scope` are
flagged. `main` is never touched and nothing is merged for you.

## Safety model (what protects the credits)
1. **Gateway caps.** The day key's cap moves daily (plan + rollover, ceiling per day). The night key has its own pool.
   The global cap holds for 30 days. Enforced by LiteLLM with Postgres. Without a DB, LiteLLM doesn't enforce budgets.
2. **Rate and parallelism caps** per key: subagent fan-out can't stampede.
3. **Loop breaker** (`PostToolUseFailure` hook): a hint at 2 failures, a stop at 3. Your next message resets it.
4. **Burst watchdog**: more than $X in Y minutes and the key gets blocked.
5. **Secret hygiene**: agent shells never hold the master key, night key or DB password. Permissions deny gateway admin calls.
6. **Replay by default** inside the agent (`MME_MODE=replay`): product commands run by the agent cost $0.
7. **Product hard stop** in `mme.client`.

## Commands
`mme help` lists everything. Highlights: `init doctor shell-init status use fallback policy today gate ship budget
credits watchdog night gateway smoke mock models hook`.

## Known gaps
- Verify on day 0 with a real key (`mme smoke`, `mme gateway check`):
  - Meta's acceptance of LiteLLM's auth headers on the Anthropic route;
  - LiteLLM `/key/info` and `/key/update` response shapes;
  - `response_format` json_schema on Chat Completions.
- Muse Code's hook file schema isn't documented yet, so the loop breaker isn't wired there. See `templates/project/mme/harness/muse-code.md`.
- Windows: use WSL (gateway scripts are bash).

## Credits
ECC by Affaan Mustafa (MIT), which MME builds on and doesn't vendor. LiteLLM by BerriAI. Meta Model API by Meta.

MIT © 2026 Romeo Lorenzo Monfroglio
