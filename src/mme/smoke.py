"""Day-0 smoke test: proves your key, model IDs and region access. Costs < $0.05.

    mme smoke            # text only (~$0.001)
    mme smoke --image    # + one Muse Image ($0.01)
    against the mock:    MODEL_API_KEY=mock-key MME_BASE_URL=http://127.0.0.1:8090/v1 mme smoke --image

It does NOT test SAM 3.1 or Voice Transcribe: their request formats must be taken
from dev.meta.ai/docs/api-reference on kickoff day (see .claude/skills/meta-model-api).
"""
import argparse
import sys
import urllib.error

from mme.client import MME, ApiError
from mme.paths import state_dir


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mme smoke")
    ap.add_argument("--image", action="store_true")
    args = ap.parse_args(argv)
    return run(args)


def run(args) -> int:

    m = MME(mode="off")  # bypass cache: we want real calls
    schema = {
        "type": "object",
        "properties": {"room": {"type": "string"}, "area_m2": {"type": "number"}},
        "required": ["room", "area_m2"],
        "additionalProperties": False,
    }
    ok = True
    for tier in ("draft", "standard"):
        model = m.model_for(tier)
        try:
            out = m.chat_json([{"role": "user", "content": "Return a kitchen of 12 square meters as JSON."}],
                              json_schema=schema, tier=tier, tag="smoke", max_tokens=1024)  # Spark reasons first (~250 tokens)
            print(f"OK   {model:<30} structured output -> {out}")
        except ApiError as e:
            ok = False
            print(f"FAIL {model:<30} {e}")
            if tier == "draft":
                print("     (contributor tier may not be offered in your country; set MME_PRIVATE=1)")
        except urllib.error.URLError as e:
            ok = False
            print(f"FAIL {model:<30} network: {e.reason}  <- check internet/VPN/proxy, not the API")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL {model:<30} {type(e).__name__}: {e}  <- check json_schema support in docs")

    if args.image:
        try:
            imgs = m.image_generate("Minimal Scandinavian retail store interior, eye level, daylight", tag="smoke")
            out = state_dir() / "smoke.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(imgs[0])
            print(f"OK   muse-image-1.0                 -> {out}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL muse-image-1.0  {e}")

    print(f"\nLedger total: ${m.spent():.4f}")
    return 0 if ok else 1
