#!/usr/bin/env bash
# Weekend fire drill: the whole gateway against the LOCAL MOCK. Costs $0, needs Docker.
#   mme gateway drill
# Proves: routing through LiteLLM, which auth header reaches "Meta", spend is priced, daily budget blocks,
# the watchdog blocks a burst, the inside-Meta fallback works on 429, and spend survives a restart.
# Leaves your real config untouched at the end (re-renders it) and deletes the drill key.
set -uo pipefail
: "${MME_PKG:?run via: mme gateway drill}" "${MME_BIN:?}" "${LITELLM_MASTER_KEY:?}" "${POSTGRES_PASSWORD:?}"
cd "$MME_PKG/gateway"
GW=http://127.0.0.1:4000; MOCK=http://127.0.0.1:8090
M="Authorization: Bearer $LITELLM_MASTER_KEY"
pass=0; fail=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
scenario() { curl -fsS "$MOCK/_mock/scenario" -H 'content-type: application/json' -d "$1" >/dev/null; }
spend() { curl -fsS "$GW/key/info?key=$1" -H "$M" | python3 -c 'import json,sys;i=json.load(sys.stdin);print((i.get("info") or i).get("spend") or 0)'; }
call() {  # $1 key, $2 model -> http code
  curl -sS -o /tmp/drill.json -w '%{http_code}' "$GW/v1/messages" -H "Authorization: Bearer $1" \
    -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
    -d "{\"model\":\"$2\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"drill\"}]}"
}

echo "== 1. Up with mock upstream"
docker volume inspect mme-litellm-db >/dev/null 2>&1 || docker volume create mme-litellm-db >/dev/null
$MME_BIN gateway render --mock || exit 1
docker compose --profile mock up -d --wait || { echo "compose failed"; exit 1; }
docker compose restart litellm >/dev/null && docker compose up -d --wait litellm >/dev/null

KEY=$(curl -fsS "$GW/key/generate" -H "$M" -H 'content-type: application/json' \
  -d '{"key_alias":"drill","models":["muse-spark-1.3-contributor","muse-spark-1.3","muse-wonder-1.0"],"max_budget":0.5,"budget_duration":"1d","max_parallel_requests":4}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["key"])') || { echo "key mint failed"; exit 1; }

echo "== 2. Basic routing"
scenario '{"mode":"normal","cached_ratio":0.5}'
[ "$(call "$KEY" muse-spark-1.3-contributor)" = 200 ] && ok "messages via gateway" || bad "messages via gateway: $(head -c 200 /tmp/drill.json)"
AUTH=$(curl -fsS "$MOCK/_mock/stats" | python3 -c 'import json,sys;s=json.load(sys.stdin)["stats"];print(",".join(k for k in s if k.startswith("auth:")))')
echo "      auth header LiteLLM sent upstream: $AUTH  (real Meta wants bearer; if only x-api-key, plan for --openai)"

echo "== 3. Spend is priced"
sleep 3
S=$(spend "$KEY"); python3 -c "import sys;sys.exit(0 if float('$S')>0 else 1)" && ok "spend recorded (\$$S)" || bad "spend is 0: model_info pricing not applied"

echo "== 4. Inside-Meta fallback on 429 (contributor -> standard)"
scenario '{"mode":"rate_limit","every":1,"models":["muse-spark-1.3-contributor"]}'
code=$(call "$KEY" muse-spark-1.3-contributor)
scenario '{"mode":"normal","models":[]}'
[ "$code" = 200 ] && grep -q 'mock muse-spark-1.3]' /tmp/drill.json && ok "429 on contributor -> answered by muse-spark-1.3" \
  || bad "fallback not working (HTTP $code: $(head -c 200 /tmp/drill.json)); check router_settings.fallbacks"

echo "== 5. Watchdog blocks a burst"
MME_GATEWAY_KEY=$KEY timeout 60 $MME_BIN watchdog --usd 0.05 --minutes 5 --poll 2 --no-budget-sync > /tmp/drill_wd.log 2>&1 &
WD=$!; sleep 3
scenario '{"mode":"burn","burn_tokens":300000,"cached_ratio":0}'
for _ in 1 2 3; do call "$KEY" muse-spark-1.3 >/dev/null; sleep 3; done
wait $WD 2>/dev/null
grep -q BLOCKED /tmp/drill_wd.log && ok "watchdog blocked the key" || bad "watchdog did not trip: $(tail -2 /tmp/drill_wd.log)"
[ "$(call "$KEY" muse-spark-1.3)" != 200 ] && ok "blocked key is refused" || bad "blocked key still works"
curl -fsS "$GW/key/unblock" -H "$M" -H 'content-type: application/json' -d "{\"key\":\"$KEY\"}" >/dev/null

echo "== 6. Daily budget hard stop"
for _ in 1 2 3 4 5; do code=$(call "$KEY" muse-wonder-1.0); [ "$code" != 200 ] && break; done
grep -qi budget /tmp/drill.json && ok "budget exceeded -> refused (HTTP $code)" || bad "budget never enforced (is the DB connected?)"

echo "== 7. Spend survives a restart"
BEFORE=$(spend "$KEY")
docker compose restart litellm db >/dev/null && docker compose up -d --wait litellm >/dev/null
AFTER=$(spend "$KEY")
[ "$BEFORE" = "$AFTER" ] && ok "spend persisted across restart (\$$AFTER)" || bad "spend changed: $BEFORE -> $AFTER"

echo "== Cleanup"
scenario '{"mode":"normal","models":[]}'
curl -fsS "$GW/key/delete" -H "$M" -H 'content-type: application/json' -d "{\"keys\":[\"$KEY\"]}" >/dev/null && echo "drill key deleted"
docker compose --profile mock stop meta-mock >/dev/null
if [ -n "${MODEL_API_KEY:-}" ]; then $MME_BIN gateway render && docker compose restart litellm >/dev/null && echo "real config restored"; fi
echo; echo "drill: $pass passed, $fail failed"
[ $fail = 0 ]
