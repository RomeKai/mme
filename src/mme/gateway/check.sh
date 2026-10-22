#!/usr/bin/env bash
# Verifies the gateway end to end with ONE tiny request per model (~$0.0002 total).
#   mme gateway check
# If backend A fails with 401/403, the provider rejects the auth LiteLLM sends: run `mme gateway up --openai`.
set -uo pipefail
: "${MME_GATEWAY_KEY:?run mme gateway up and add MME_GATEWAY_KEY to .env}" "${LITELLM_MASTER_KEY:?}"
GW=http://127.0.0.1:4000
echo "LiteLLM version: $(curl -fsS $GW/health/readiness | "$MME_PYTHON" -c 'import json,sys;print(json.load(sys.stdin).get("litellm_version","?"))' 2>/dev/null)"

ok=1
for m in $("$MME_PYTHON" -c 'from mme import providers; print(" ".join(providers.load()["gateway"]["models"][:2]))'); do
  code=$(curl -sS -o /tmp/mme_check.json -w '%{http_code}' $GW/v1/messages \
    -H "Authorization: Bearer $MME_GATEWAY_KEY" -H "content-type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    -d "{\"model\":\"$m\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with: ok\"}]}")
  if [ "$code" = "200" ]; then echo "OK   $m via /v1/messages"; else ok=0; echo "FAIL $m HTTP $code: $(head -c 300 /tmp/mme_check.json)"; fi
done

echo "Key spend so far:"
curl -fsS "$GW/key/info?key=$MME_GATEWAY_KEY" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | "$MME_PYTHON" -c 'import json,sys; i=json.load(sys.stdin).get("info",{}); print(f"  spend=${i.get(\"spend\")}  max_budget=${i.get(\"max_budget\")}  resets={i.get(\"budget_reset_at\")}")'
echo "If spend is 0 after the calls above, pricing isn't being applied: check model_info in the config."
[ $ok = 1 ]
