"""MME client: the ONLY module your product should use to talk to the Meta Model API.

Zero third-party dependencies (stdlib urllib) so it works anywhere.
Features that protect the $150 credit pool:
  * tiers  -> one place decides which model a call uses (draft / standard / max)
  * cache  -> identical requests are served from disk for $0 (content hash)
  * replay -> MME_MODE=replay never hits the network (tests, CI, demo fallback)
  * ledger -> every paid call is appended to .mme/ledger.jsonl with its real cost
  * budget -> hard stop before a call once MME_HARD_STOP_USD is reached
  * retry  -> exponential backoff on 429 / 5xx

Env:
  MODEL_API_KEY           API key (never commit it)
  MME_BASE_URL            default https://api.meta.ai/v1
  MME_MODE                live (default) | replay | off  (off = cache disabled, still live)
  MME_PRIVATE=1           never use *-contributor models (their data may train Meta models)
  MME_HARD_STOP_USD       default 100 (product spend cap; leaves room for the harness + demo)
  MME_MODEL_DRAFT / MME_MODEL_STANDARD / MME_MODEL_MAX   override tier models
  MME_HOME                where cache/ledger live, default <project>/.mme
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mme import providers
from mme.paths import state_dir

Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class BudgetExceeded(RuntimeError):
    pass


class ReplayMiss(RuntimeError):
    pass


class EmptyResponse(RuntimeError):
    """The model returned no content, e.g. max_tokens was spent on reasoning before the answer."""


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Meta Model API error {status}: {body[:500]}")
        self.status = status


def _default_home() -> Path:
    return state_dir()


ASR_MODEL = "muse-voice-transcribe-1.0"
ASR_RATES = (16000, 24000)   # the endpoint takes RIFF/WAVE, mono, 16-bit PCM at 16 or 24 kHz


def encode_multipart(parts: dict[str, Any]) -> tuple[bytes, str]:
    """multipart/form-data body for /asr/transcribe: `request` as a JSON part, `audio` as a WAV file part."""
    boundary = "mme" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    out = b""
    for name, value in parts.items():
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, bytes):
            out += (f'Content-Disposition: form-data; name="{name}"; filename="{name}.wav"\r\n'
                    "Content-Type: audio/wav\r\n\r\n").encode() + value + b"\r\n"
        else:
            out += (f'Content-Disposition: form-data; name="{name}"\r\n'
                    f"Content-Type: application/json\r\n\r\n{json.dumps(value)}\r\n").encode()
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def wav_seconds(audio: bytes) -> float:
    """Duration of a WAV the endpoint accepts; ValueError for anything else, before any paid call."""
    import io
    import wave
    try:
        with wave.open(io.BytesIO(audio)) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a WAV file: {e}") from None
    if channels != 1 or width != 2 or rate not in ASR_RATES:
        raise ValueError(f"need mono 16-bit PCM at 16/24 kHz, got {channels} ch, {8 * width}-bit, {rate} Hz")
    return frames / rate


def _urllib_transport(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    if "__multipart__" in payload:
        body, ctype = encode_multipart(payload["__multipart__"])
        headers = {**headers, "Content-Type": ctype, "Accept": "application/json"}
    else:
        body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:  # surface status for retry logic
        raise ApiError(e.code, e.read().decode(errors="replace")) from None


@dataclass
class MME:
    api_key: str | None = field(default_factory=lambda: os.environ.get("MODEL_API_KEY"))
    base_url: str = field(default_factory=lambda: os.environ.get("MME_BASE_URL", providers.load()["base_url"]).rstrip("/"))
    mode: str = field(default_factory=lambda: os.environ.get("MME_MODE", "live"))
    hard_stop_usd: float = field(default_factory=lambda: float(os.environ.get("MME_HARD_STOP_USD", "100")))
    private: bool = field(default_factory=lambda: os.environ.get("MME_PRIVATE") == "1")
    home: Path = field(default_factory=_default_home)
    transport: Transport = _urllib_transport
    max_retries: int = 5
    sleep: Callable[[float], None] = time.sleep

    # ---------- tiers ----------
    def model_for(self, tier: str) -> str:
        """draft = iteration/tests, standard = evals/demo recordings, max = only when standard measurably fails."""
        return providers.tier_model(tier, private=self.private)

    # ---------- ledger / budget ----------
    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.jsonl"

    def spent(self) -> float:
        if not self.ledger_path.exists():
            return 0.0
        total = 0.0
        for line in self.ledger_path.read_text().splitlines():
            if line.strip():
                total += json.loads(line).get("cost_usd", 0.0)
        return total

    def _check_budget(self) -> None:
        spent = self.spent()
        if spent >= self.hard_stop_usd:
            raise BudgetExceeded(f"MME spend ${spent:.2f} >= hard stop ${self.hard_stop_usd:.2f}")

    def record(self, *, model: str, cost_usd: float, tag: str, **extra: Any) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "cost_usd": round(cost_usd, 6), "tag": tag, **extra}
        with self.ledger_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    # ---------- cache ----------
    def _cache_file(self, endpoint: str, payload: dict[str, Any]) -> Path:
        key = hashlib.sha256(json.dumps([endpoint, payload], sort_keys=True).encode()).hexdigest()
        return self.home / "cache" / key[:2] / f"{key}.json"

    # ---------- core ----------
    def _post(self, endpoint: str, payload: dict[str, Any], *, use_cache: bool,
              wire: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """`payload` keys the cache; `wire` (default: payload) is what the transport sends, e.g. raw audio bytes."""
        cache_file = self._cache_file(endpoint, payload)
        if use_cache and self.mode != "off" and cache_file.exists():
            return json.loads(cache_file.read_text()), True
        if self.mode == "replay":
            raise ReplayMiss(f"no cached response for {endpoint} ({cache_file.name}); record it in live mode first")
        if not self.api_key:
            raise RuntimeError("MODEL_API_KEY is not set")
        self._check_budget()

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            try:
                data = self.transport(url, payload if wire is None else wire, headers)
                break
            except ApiError as e:
                if e.status not in (408, 409, 429, 500, 502, 503, 504) or attempt == self.max_retries:
                    raise
                self.sleep(min(30.0, 2**attempt) + random.random())
        if use_cache and self.mode != "off":
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
        return data, False

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: str = "draft",
        tag: str = "untagged",
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "output",
        use_cache: bool = True,
        **params: Any,
    ) -> dict[str, Any]:
        """OpenAI-compatible Chat Completions call. Returns the raw response.

        Keep the system prompt + few-shot examples FIRST and identical across calls:
        Meta caches stable prefixes automatically (cached input is ~8-50x cheaper).
        """
        model = self.model_for(tier)
        payload: dict[str, Any] = {"model": model, "messages": messages, **params}
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            }
        data, hit = self._post("chat/completions", payload, use_cache=use_cache)
        if not hit:
            u = data.get("usage") or {}
            inp = int(u.get("prompt_tokens", 0))
            cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            out = int(u.get("completion_tokens", 0))
            self.record(model=model, cost_usd=providers.text_cost(model, inp, cached, out), tag=tag,
                        input_tokens=inp, cached_tokens=cached, output_tokens=out)
        return data

    def chat_json(self, messages: list[dict[str, Any]], *, json_schema: dict[str, Any], **kw: Any) -> Any:
        """Chat call that returns parsed JSON (validate it with pydantic at the call site)."""
        data = self.chat(messages, json_schema=json_schema, **kw)
        choice = data["choices"][0]
        content = choice["message"].get("content")
        if content is None:
            usage = data.get("usage") or {}
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", "?")
            raise EmptyResponse(
                f"no content (finish_reason={choice.get('finish_reason')}, completion_tokens="
                f"{usage.get('completion_tokens')}, reasoning_tokens={reasoning}): Spark reasons before answering, "
                "raise max_tokens")
        return json.loads(content)

    def respond(
        self,
        input: str | list[dict[str, Any]],
        *,
        tier: str = "draft",
        tag: str = "untagged",
        tools: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        previous_response_id: str | None = None,
        use_cache: bool = True,
        **params: Any,
    ) -> dict[str, Any]:
        """Responses API call (`POST /v1/responses`): Meta's recommended default for multi-turn agents.

        Pass `previous_response_id` to keep the model's reasoning across turns, which Chat Completions loses.
        Returns the raw response; use `output_text()` / `tool_calls()` to read it.
        """
        model = self.model_for(tier)
        payload: dict[str, Any] = {"model": model, "input": input, **params}
        if tools:
            payload["tools"] = tools
        if instructions:
            payload["instructions"] = instructions
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        data, hit = self._post("responses", payload, use_cache=use_cache)
        if not hit:
            u = data.get("usage") or {}
            inp = int(u.get("input_tokens", 0))
            cached = int((u.get("input_tokens_details") or {}).get("cached_tokens", 0))
            out = int(u.get("output_tokens", 0))
            self.record(model=model, cost_usd=providers.text_cost(model, inp, cached, out), tag=tag,
                        input_tokens=inp, cached_tokens=cached, output_tokens=out)
        return data

    @staticmethod
    def output_text(response: dict[str, Any]) -> str:
        parts = []
        for item in response.get("output", []):
            if item.get("type") == "message":
                parts += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
        return "".join(parts)

    @staticmethod
    def tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
        """[{call_id, name, arguments(dict)}] for every function call in the response."""
        calls = []
        for item in response.get("output", []):
            if item.get("type") == "function_call":
                args = item.get("arguments") or "{}"
                calls.append({"call_id": item.get("call_id") or item.get("id"), "name": item.get("name"),
                              "arguments": json.loads(args) if isinstance(args, str) else args})
        return calls

    def transcribe(self, audio: bytes, *, tag: str = "asr", language_bias: list[str] | None = None,
                   keywords: list[str] | None = None, mode: str = "PUSH_TO_TALK", use_cache: bool = True) -> dict[str, Any]:
        """Muse Voice Transcribe (`POST /v1/asr/transcribe`, multipart). `audio`: WAV mono 16-bit at 16/24 kHz.
        `language_bias` e.g. ["Spanish"] steers recognition; `keywords` bias rare terms. $0.00005 per second."""
        seconds = wav_seconds(audio)
        request: dict[str, Any] = {"model": ASR_MODEL, "audioEncoding": "WAV", "mode": mode}
        if language_bias:
            request["languageBias"] = language_bias
        if keywords:
            request["keywords"] = keywords
        key = {"request": request, "audio_sha256": hashlib.sha256(audio).hexdigest()}
        data, hit = self._post("asr/transcribe", key, use_cache=use_cache,
                               wire={"__multipart__": {"request": request, "audio": audio}})
        if not hit:
            self.record(model=ASR_MODEL, cost_usd=providers.unit_cost(ASR_MODEL, seconds), tag=tag, seconds=round(seconds, 2))
        return data

    @staticmethod
    def transcript_text(response: dict[str, Any]) -> str:
        """Final text of a transcribe response: the top-level transcript, else the turns joined."""
        if isinstance(response.get("transcript"), str):
            return response["transcript"]
        turns = response.get("turns") or response.get("results") or []
        return " ".join(t.get("transcript", "") for t in turns if t.get("transcript")).strip()

    def record_unit(self, model: str, units: float, *, tag: str, kind: str = "usd") -> None:
        """Ledger entry for unit-priced models the client doesn't call itself (SAM 3.1)."""
        self.record(model=model, cost_usd=providers.unit_cost(model, units, kind), tag=tag, units=units)

    def image_generate(self, prompt: str, *, tag: str = "render", n: int = 1, use_cache: bool = True, **params: Any) -> list[bytes]:
        """Muse Image text->image. Returns PNG/JPEG bytes. $0.01 per returned image."""
        payload = {"model": "muse-image-1.0", "prompt": prompt, "n": n, "response_format": "b64_json", **params}
        data, hit = self._post("images/generations", payload, use_cache=use_cache)
        images = [base64.b64decode(d["b64_json"]) for d in data.get("data", []) if d.get("b64_json")]
        if not hit:
            self.record(model="muse-image-1.0", cost_usd=providers.unit_cost("muse-image-1.0", len(images)), tag=tag, images=len(images))
        return images


_default: MME | None = None


def client() -> MME:
    global _default
    if _default is None:
        _default = MME()
    return _default
