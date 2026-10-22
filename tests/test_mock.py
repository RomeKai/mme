"""The mock must look like Meta to our clients: mme.client (chat/images), Anthropic SSE, auth, errors."""
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mme.client import MME, ApiError, BudgetExceeded  # noqa: E402,F401
from mme.mock import meta_mock as mock  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.httpd = mock.serve(cls.port)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.scenario({"mode": "normal", "every": 2, "cached_ratio": 0.5, "burn_tokens": 2_000_000})
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json", "Authorization": "Bearer mock-key", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    def scenario(self, s):
        req = urllib.request.Request(self.base + "/_mock/scenario", data=json.dumps(s).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req).read()

    def client(self, **kw):
        return MME(api_key="mock-key", base_url=self.base + "/v1", home=Path(self.tmp.name),
                   mode="live", sleep=lambda s: None, **kw)

    def test_llm_chat_records_cost_with_cache(self):
        m = self.client()
        m.chat([{"role": "user", "content": "hola"}], tag="t")
        row = json.loads(m.ledger_path.read_text().splitlines()[0])
        self.assertEqual(row["model"], "muse-spark-1.3-contributor")
        self.assertGreater(row["cached_tokens"], 0)
        self.assertGreater(row["cost_usd"], 0)

    def test_llm_images(self):
        imgs = self.client().image_generate("store", n=2)
        self.assertEqual(len(imgs), 2)
        self.assertTrue(imgs[0].startswith(b"\x89PNG"))

    def test_burn_mode_trips_hard_stop(self):
        self.scenario({"mode": "burn", "burn_tokens": 5_000_000, "cached_ratio": 0})
        m = self.client(hard_stop_usd=1.0)
        m.chat([{"role": "user", "content": "a"}], tier="max", use_cache=False)   # Wonder: $20 + $25 -> way over
        with self.assertRaises(BudgetExceeded):
            m.chat([{"role": "user", "content": "b"}], tier="max", use_cache=False)

    def test_rate_limit_is_retried(self):
        self.scenario({"mode": "rate_limit", "every": 2})
        m = self.client()
        for i in range(3):
            m.chat([{"role": "user", "content": f"r{i}"}], use_cache=False)  # some calls hit 429 and get retried

    def test_rate_limit_only_on_selected_models(self):
        self.scenario({"mode": "rate_limit", "every": 1, "models": ["muse-spark-1.3-contributor"]})
        body = {"model": "muse-spark-1.3", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]}
        self.assertEqual(self.post("/v1/messages", body).status, 200)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/messages", {**body, "model": "muse-spark-1.3-contributor"})
        self.assertEqual(ctx.exception.code, 429)
        self.scenario({"models": []})

    def test_unknown_model_404_like_meta(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {"model": "gpt-x", "messages": []})
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_key_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/messages", {"model": "muse-spark-1.3"}, {"Authorization": "Bearer nope"})
        self.assertEqual(ctx.exception.code, 401)

    def test_x_api_key_accepted_and_counted(self):
        r = self.post("/v1/messages", {"model": "muse-spark-1.3", "max_tokens": 5,
                                       "messages": [{"role": "user", "content": "x"}]},
                      {"Authorization": "", "x-api-key": "mock-key"})
        self.assertEqual(json.load(r)["type"], "message")
        stats = json.load(urllib.request.urlopen(self.base + "/_mock/stats"))
        self.assertGreaterEqual(stats["stats"].get("auth:x-api-key", 0), 1)

    def test_anthropic_stream_has_usage_events(self):
        r = self.post("/v1/messages", {"model": "muse-spark-1.3-contributor", "stream": True, "max_tokens": 5,
                                       "messages": [{"role": "user", "content": "x"}]})
        text = r.read().decode()
        events = [line[7:] for line in text.splitlines() if line.startswith("event: ")]
        self.assertEqual(events[0], "message_start")
        self.assertIn("message_delta", events)
        self.assertEqual(events[-1], "message_stop")
        self.assertIn('"cache_read_input_tokens"', text)

    def test_openai_stream_with_usage_and_done(self):
        r = self.post("/v1/chat/completions", {"model": "muse-spark-1.3", "stream": True,
                                               "stream_options": {"include_usage": True}, "messages": []})
        lines = [l for l in r.read().decode().splitlines() if l.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assertIn('"usage"', lines[-2])


if __name__ == "__main__":
    unittest.main()
