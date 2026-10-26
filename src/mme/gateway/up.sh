#!/usr/bin/env bash
# mme gateway up [--openai | --mock]
# Brings up the gateway and mints two keys:
#   day key   (MME_GATEWAY_KEY) cumulative spend; `mme budget sync` moves its cap daily (rollover)
#   night key (MME_NIGHT_KEY)   its own pool for unattended missions, 2 parallel requests max
set -euo pipefail
: "${MME_PKG:?run via: mme gateway up}" "${MME_BIN:?}" "${MME_PYTHON:?}" "${LITELLM_MASTER_KEY:?}" "${POSTGRES_PASSWORD:?}"
cd "$MME_PKG/gateway"
PARALLEL="${MME_GATEWAY_PARALLEL:-4}"
read -r DAY_CAP NIGHT_POOL < <("$MME_PYTHON" -m mme budget caps | tr -d '\r')  # Windows python prints CRLF
MODELS=$("$MME_PYTHON" -c 'import json; from mme import providers; print(json.dumps(providers.load()["gateway"]["models"]))')

docker volume inspect mme-litellm-db >/dev/null 2>&1 || docker volume create mme-litellm-db >/dev/null
"$MME_PYTHON" -m mme gateway render "$@"
if [[ " $* " == *" --mock "* ]]; then
  echo "MOCK upstream: nothing reaches Meta; spend is simulated. Mint fresh keys before kickoff (see RUNBOOK)."
  docker compose --profile mock up -d --wait
else
  docker compose up -d --wait litellm
fi
echo "gateway healthy on http://127.0.0.1:4000"

# Observation-only budget window (never binding): makes /key/info report the live spend counter,
# so the watchdog does not wait for LiteLLM's batched DB writes.
WATCH_WINDOW='[{"budget_duration": "1d", "max_budget": 1000000}]'
mint() {  # alias max_budget parallel
  curl -fsS http://127.0.0.1:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
    -d "{\"key_alias\": \"$1\", \"models\": $MODELS, \"max_budget\": $2, \"rpm_limit\": 60, \"max_parallel_requests\": $3, \"budget_limits\": $WATCH_WINDOW}" \
    | "$MME_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["key"])'
}
[ -z "${MME_GATEWAY_KEY:-}" ] && echo "MME_GATEWAY_KEY=$(mint harness "$DAY_CAP" "$PARALLEL")   # add to .env (today's cap \$$DAY_CAP)" || echo "MME_GATEWAY_KEY already set."
[ -z "${MME_NIGHT_KEY:-}" ] && echo "MME_NIGHT_KEY=$(mint night "$NIGHT_POOL" 2)   # add to .env (night pool \$$NIGHT_POOL)" || echo "MME_NIGHT_KEY already set."
echo "Then reload .env and run: mme budget sync"
