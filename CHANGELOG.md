# Changelog

## 0.2.0 — 2026-10-22
- Standalone package with a single `mme` CLI, `mme init` and `mme doctor`.
- Provider catalog (`providers/meta.json`) as the single source of model IDs, prices and limits. The gateway config is generated from it.
- Night **missions**: briefing (`mme night prep`), templates (feature/fix/tests/single/custom), staged runs with
  one repair attempt, cheap read-only review (capped), scope check, reservation-based per-night cap.
- Delivery gates: `milestones.yaml`, `mme today`, `mme gate`, `mme ship`.
- `mme credits` combines the product ledger and both gateway keys.
- Security: agent shells stripped of admin secrets; permissions deny gateway admin endpoints and MME controls.
- Budget: `prelaunch_usd`, nested `night` settings (`review_max_usd`).

## 0.1.0 — 2026-10-22
- First kit inside ArchProto: harness policies, LiteLLM gateway with budgets, loop breaker, watchdog, mock and drill,
  variable budget, simple night tasks.
