"""Local mock of the Meta Model API, for plumbing tests (gateway, budgets, watchdog, retries) at $0.

It imitates SHAPES, not intelligence: replies are canned text. Model quality, real prices and real
limits must still be checked on the real API (scripts/smoke_meta.py, mme/gateway/check.sh).

Endpoints (same paths as api.meta.ai):
  POST /v1/messages                 Anthropic format, JSON or SSE stream (Claude Code / LiteLLM backend A)
  POST /v1/messages/count_tokens    rough estimate
  POST /v1/chat/completions         OpenAI format, JSON or SSE stream (llm.py / LiteLLM backend B)
  POST /v1/responses                minimal Responses shape
  POST /v1/images/generations       1x1 PNG per image
  GET  /v1/models                   known model IDs
Control (not in the real API):
  POST /_mock/scenario   {"mode": "normal|burn|rate_limit|error|slow", "every": 2, "burn_tokens": 2000000,
                          "cached_ratio": 0.5, "delay_s": 3, "models": ["only-these-fail"]}
  GET  /_mock/stats      counts per model / endpoint / auth header seen
  GET  /health

Run:  mme mock --port 8090      (key: MOCK_API_KEY, default "mock-key")
Stdlib only. Model IDs come from the provider catalog, so the mock 404s exactly what Meta would.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from mme import providers
    MODELS = providers.all_model_ids()
except ImportError:  # standalone inside the gateway's mock container: the provider file is mounted read-only
    import pathlib
    _p = json.loads(pathlib.Path(os.environ.get("MOCK_PROVIDER_FILE", "/provider/meta.json")).read_text())
    MODELS = [*_p["text_models"], *_p["unit_models"]]
PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffffff7f0009fb03fd2a86e38a0000000049454e44ae426082"
)).decode()


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scenario: dict = {"mode": "normal", "every": 2, "burn_tokens": 2_000_000, "cached_ratio": 0.5, "delay_s": 3}
        self.calls = 0
        self.stats: Counter = Counter()

    def tick(self, key: str) -> None:
        with self.lock:
            self.stats[key] += 1

    def next_request(self) -> int:
        with self.lock:
            self.calls += 1
            return self.calls


STATE = State()
API_KEY = os.environ.get("MOCK_API_KEY", "mock-key")


def est_tokens(obj) -> int:
    return max(1, len(json.dumps(obj)) // 4)


def usage_numbers(body: dict) -> tuple[int, int, int]:
    """(input, cached, output) according to the active scenario."""
    sc = STATE.scenario
    if sc["mode"] == "burn":
        inp = int(sc["burn_tokens"])
        out = inp // 4
    else:
        inp = est_tokens(body.get("messages") or body.get("input") or body)
        out = 12
    return inp, int(inp * float(sc["cached_ratio"])), out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        if os.environ.get("MOCK_VERBOSE"):
            super().log_message(*args)

    # ---------- helpers ----------
    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events: list[tuple[str | None, dict | str]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, payload in events:
            chunk = (f"event: {name}\n" if name else "") + "data: " + (payload if isinstance(payload, str) else json.dumps(payload)) + "\n\n"
            self.wfile.write(chunk.encode())
            self.wfile.flush()
        self.close_connection = True

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        xkey = self.headers.get("x-api-key", "")
        if auth == f"Bearer {API_KEY}":
            STATE.tick("auth:bearer")
            return True
        if xkey == API_KEY:
            STATE.tick("auth:x-api-key")
            return True
        self._json(401, {"error": {"type": "authentication_error", "message": "invalid API key"}})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _scenario_gate(self, model: str | None = None) -> bool:
        """Returns False if the scenario already answered (error / rate limit).

        Optional "models": [...] in the scenario limits errors/429s to those models (to test fallbacks).
        """
        sc = STATE.scenario
        n = STATE.next_request()
        if sc.get("models") and model not in sc["models"]:
            return True
        if sc["mode"] == "slow":
            time.sleep(float(sc["delay_s"]))
        if sc["mode"] == "rate_limit" and n % int(sc["every"]) == 0:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "application/json")
            body = b'{"error":{"type":"rate_limit_error","message":"mock rate limit"}}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        if sc["mode"] == "error" and n % int(sc["every"]) == 0:
            self._json(500, {"error": {"type": "api_error", "message": "mock internal error"}})
            return False
        return True

    def _model(self, body: dict) -> str | None:
        m = body.get("model")
        if m not in MODELS:
            self._json(404, {"error": {"type": "not_found_error", "message": f"Model not found: {m}"}})
            return None
        return m

    # ---------- routes ----------
    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/_mock/stats":
            return self._json(200, {"calls": STATE.calls, "stats": dict(STATE.stats), "scenario": STATE.scenario})
        if self.path == "/v1/models":
            if not self._auth():
                return
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path == "/_mock/scenario":
            STATE.scenario.update(self._body())
            return self._json(200, STATE.scenario)
        if not self._auth():
            return
        body = self._body()
        STATE.tick(f"endpoint:{self.path}")
        routes = {
            "/v1/messages": self.messages,
            "/v1/messages/count_tokens": self.count_tokens,
            "/v1/chat/completions": self.chat,
            "/v1/responses": self.responses_api,
            "/v1/images/generations": self.images,
        }
        fn = routes.get(self.path.split("?")[0])
        if fn is None:
            return self._json(404, {"error": {"message": f"no mock for {self.path}"}})
        fn(body)

    def count_tokens(self, body: dict) -> None:
        self._json(200, {"input_tokens": est_tokens(body.get("messages", []))})

    def messages(self, body: dict) -> None:
        model = self._model(body)
        if not model or not self._scenario_gate(model):
            return
        STATE.tick(f"model:{model}")
        inp, cached, out = usage_numbers(body)
        text = f"[mock {model}] ok"
        mid = f"msg_{uuid.uuid4().hex[:24]}"
        usage_in = {"input_tokens": inp - cached, "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
        if body.get("stream"):
            return self._sse([
                ("message_start", {"type": "message_start", "message": {
                    "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
                    "stop_reason": None, "usage": {**usage_in, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": out}}),
                ("message_stop", {"type": "message_stop"}),
            ])
        self._json(200, {"id": mid, "type": "message", "role": "assistant", "model": model,
                         "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                         "usage": {**usage_in, "output_tokens": out}})

    def chat(self, body: dict) -> None:
        model = self._model(body)
        if not model or not self._scenario_gate(model):
            return
        STATE.tick(f"model:{model}")
        inp, cached, out = usage_numbers(body)
        content = "{}" if body.get("response_format", {}).get("type") == "json_schema" else f"[mock {model}] ok"
        usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out,
                 "prompt_tokens_details": {"cached_tokens": cached}}
        cid, now = f"chatcmpl-{uuid.uuid4().hex[:20]}", int(time.time())
        if body.get("stream"):
            base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model}
            events = [
                (None, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}),
                (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            ]
            if (body.get("stream_options") or {}).get("include_usage"):
                events.append((None, {**base, "choices": [], "usage": usage}))
            events.append((None, "[DONE]"))
            return self._sse(events)
        self._json(200, {"id": cid, "object": "chat.completion", "created": now, "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                         "usage": usage})

    def responses_api(self, body: dict) -> None:  # not "responses": that name is a base-class status table
        model = self._model(body)
        if not model or not self._scenario_gate(model):
            return
        STATE.tick(f"model:{model}")
        inp, cached, out = usage_numbers(body)
        self._json(200, {"id": f"resp_{uuid.uuid4().hex[:20]}", "object": "response", "model": model, "status": "completed",
                         "output": [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": f"[mock {model}] ok"}]}],
                         "usage": {"input_tokens": inp, "output_tokens": out,
                                   "input_tokens_details": {"cached_tokens": cached}}})

    def images(self, body: dict) -> None:
        if body.get("model") != "muse-image-1.0":
            return self._json(404, {"error": {"message": f"Model not found: {body.get('model')}"}})
        if not self._scenario_gate("muse-image-1.0"):
            return
        STATE.tick("model:muse-image-1.0")
        n = int(body.get("n", 1))
        self._json(200, {"created": int(time.time()), "data": [{"b64_json": PNG_1PX} for _ in range(n)]})


def serve(port: int = 8090, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mme mock")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MOCK_PORT", "8090")))
    ap.add_argument("--host", default=os.environ.get("MOCK_HOST", "127.0.0.1"))
    a = ap.parse_args(argv)
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[meta-mock] listening on http://{a.host}:{a.port}  key={API_KEY!r}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
