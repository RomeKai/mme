#!/usr/bin/env bash
# Dump the gateway DB (keys, budgets, spend logs) to .mme/backups/. Run before risky changes.
#   mme gateway backup
set -euo pipefail
: "${MME_PKG:?run via mme gateway backup}" "${MME_STATE:?}"
cd "$MME_PKG/gateway"
mkdir -p "$MME_STATE/backups"
out="$MME_STATE/backups/litellm-$(date +%Y%m%d-%H%M).sql.gz"
docker compose exec -T db pg_dump -U litellm litellm | gzip > "$out"
echo "backup -> $out"
# Restore: gunzip -c FILE | docker compose exec -T db psql -U litellm litellm
