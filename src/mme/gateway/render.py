"""Build the LiteLLM config from the provider catalog (no hand-maintained template, no duplicated prices).

    mme gateway render            backend A: anthropic/ -> api.meta.ai (Messages API; keeps reasoning across turns)
    mme gateway render --openai   backend B: openai/ -> api.meta.ai/v1 (Chat Completions; loses reasoning across turns)
    mme gateway render --mock     backend A against the local mock container (fire drill)

Written to <project>/.mme/gateway/litellm.config.yaml (chmod 600, contains the key).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from mme import providers
from mme.paths import state_dir


def build(env: dict[str, str], openai: bool = False, mock: bool = False, provider: str = "meta") -> dict:
    p = providers.load(provider)
    host = "http://meta-mock:8090" if mock else p["anthropic_base_url"]
    base = f"{host}/v1" if openai else host
    key = env.get("MOCK_API_KEY", "mock-key") if mock else env.get(p["key_env"])
    if not key:
        raise KeyError(p["key_env"])
    if not env.get("LITELLM_MASTER_KEY"):
        raise KeyError("LITELLM_MASTER_KEY")
    model_list = []
    for mid in p["gateway"]["models"]:
        m = p["text_models"][mid]
        model_list.append({
            "model_name": mid,
            "litellm_params": {
                "model": f"{'openai' if openai else 'anthropic'}/{mid}",
                "api_base": base,
                "api_key": key,
                # Meta expects a bearer token; LiteLLM's anthropic provider sends x-api-key. Send both.
                "extra_headers": {"Authorization": f"Bearer {key}"},
                # Meta rejects these with 400; `drop_params` doesn't strip them on /v1/messages pass-through.
                # Claude Code's auto-mode classifier sends `stop_sequences` (verified 2026-10-26).
                "additional_drop_params": ["stop_sequences"],
            },
            "model_info": {
                "input_cost_per_token": m["input"] / 1e6,
                "cache_read_input_token_cost": m["cached"] / 1e6,
                "output_cost_per_token": m["output"] / 1e6,
            },
        })
    return {
        "model_list": model_list,
        "router_settings": {
            # Fallback stays INSIDE the provider (rate limits on the cheap tier). Never cross-provider mid-session:
            # a conversation full of one provider's reasoning/tool blocks can't be replayed to another.
            "fallbacks": [{k: v} for k, v in p["gateway"]["fallbacks"].items()],
        },
        "litellm_settings": {
            "max_budget": float(env.get("MME_GATEWAY_TOTAL_USD", "60")),
            "budget_duration": "30d",
            "num_retries": 2,
            "request_timeout": 600,
            "drop_params": True,
            # No response cache on purpose: agent requests never repeat byte-for-byte, and a semantic cache can return
            # code written for another file. The provider's automatic prefix cache already bills history cheaply.
        },
        "general_settings": {
            "master_key": env["LITELLM_MASTER_KEY"],
            "database_url": env.get("DATABASE_URL")
            or f"postgresql://litellm:{env.get('POSTGRES_PASSWORD', '')}@db:5432/litellm",
        },
    }


def config_path() -> Path:
    return state_dir() / "gateway" / "litellm.config.yaml"


def main(argv: list[str]) -> int:
    import yaml
    mock, openai = "--mock" in argv, "--openai" in argv
    if not os.environ.get("POSTGRES_PASSWORD"):
        print("missing POSTGRES_PASSWORD (set -a; source .env; set +a)", file=sys.stderr)
        return 2
    if not os.environ.get("LITELLM_MASTER_KEY", "").startswith("sk-"):
        print("LITELLM_MASTER_KEY must be set and start with 'sk-'", file=sys.stderr)
        return 2
    try:
        cfg = build(dict(os.environ), openai=openai, mock=mock)
    except KeyError as e:
        print(f"missing env var {e}", file=sys.stderr)
        return 2
    out = config_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(cfg, sort_keys=False))
    out.chmod(0o600)
    print(f"wrote {out} (backend {'B: openai/chat' if openai else 'A: anthropic/messages'}{', MOCK' if mock else ''})")
    return 0
