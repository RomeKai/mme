"""Provider catalog: the ONE place where model IDs, prices and limits live.

Everything else (client cost math, gateway config, harness model mapping, mock, docs tables) reads from here.
Override with MME_PROVIDER_FILE=/path/to/provider.json to try new prices before releasing them.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=8)
def load(name: str = "meta") -> dict:
    path = Path(os.environ["MME_PROVIDER_FILE"]) if os.environ.get("MME_PROVIDER_FILE") else HERE / f"{name}.json"
    return json.loads(path.read_text())


def text_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int, provider: str = "meta") -> float:
    """USD for one text call. cached_tokens is a subset of input_tokens. Unknown model -> priciest rate (fail safe)."""
    models = load(provider)["text_models"]
    p = models.get(model) or max(models.values(), key=lambda m: m["output"])
    cached = max(0, min(cached_tokens, input_tokens))
    return ((input_tokens - cached) * p["input"] + cached * p["cached"] + output_tokens * p["output"]) / 1_000_000


def unit_cost(model: str, units: float, kind: str = "usd", provider: str = "meta") -> float:
    return load(provider)["unit_models"][model][kind] * units


def tier_model(tier: str, private: bool = False, provider: str = "meta") -> str:
    tiers = load(provider)["tiers"]
    if tier not in tiers:
        raise ValueError(f"tier must be one of {sorted(tiers)}")
    model = os.environ.get(f"MME_MODEL_{tier.upper()}", tiers[tier])
    return model.removesuffix("-contributor") if private and model.endswith("-contributor") else model


def harness_model(kind: str = "cheap", provider: str = "meta") -> str:
    h = load(provider)["harness"]
    if kind == "cheap" and os.environ.get("MME_PRIVATE") == "1":
        return h["cheap_private"]
    return h[kind]


def all_model_ids(provider: str = "meta") -> list[str]:
    p = load(provider)
    return [*p["text_models"], *p["unit_models"]]


def markdown_table(provider: str = "meta") -> str:
    p = load(provider)
    rows = ["| Model | Input $/1M | Cached $/1M | Output $/1M | RPM |", "|---|---:|---:|---:|---:|"]
    for mid, m in p["text_models"].items():
        rows.append(f"| `{mid}` | {m['input']:.3f} | {m['cached']:.3f} | {m['output']:.2f} | {m['rpm']} |")
    rows += ["", "| Model | Price |", "|---|---|"]
    for mid, m in p["unit_models"].items():
        rows.append(f"| `{mid}` | ${m['usd']} per {m['unit']} |")
    rows.append(f"\n_Prices checked {p['checked']}. Sources: {', '.join(p['sources'])}_")
    return "\n".join(rows)
