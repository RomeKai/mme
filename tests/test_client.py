"""mme.client: tiers, cost math, cache, replay, budget hard stop, retries. No network (fake transport)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mme.client import MME, ApiError, BudgetExceeded, EmptyResponse, ReplayMiss  # noqa: E402
from mme.providers import text_cost  # noqa: E402


def fake_response(inp=1000, cached=0, out=500, content='{"ok": true}'):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": inp, "completion_tokens": out, "prompt_tokens_details": {"cached_tokens": cached}},
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class MMETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for k in ("MME_MODEL_DRAFT", "MME_MODEL_STANDARD", "MME_MODEL_MAX"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, responses, **kw):
        t = FakeTransport(responses)
        m = MME(api_key="k", home=Path(self.tmp.name), transport=t, sleep=lambda s: None,
                mode=kw.pop("mode", "live"), hard_stop_usd=kw.pop("hard_stop_usd", 100.0),
                private=kw.pop("private", False), **kw)
        return m, t

    def test_cost_math_uses_cached_rate(self):
        # 1M input of which 0.5M cached + 1M output on spark-1.3
        c = text_cost("muse-spark-1.3", 1_000_000, 500_000, 1_000_000)
        self.assertAlmostEqual(c, 0.625 + 0.075 + 4.25)

    def test_unknown_model_priced_as_most_expensive(self):
        self.assertAlmostEqual(text_cost("mystery", 1_000_000, 0, 0), 4.00)

    def test_tiers_and_private_mode(self):
        m, _ = self.make([])
        self.assertEqual(m.model_for("draft"), "muse-spark-1.3-contributor")
        self.assertEqual(m.model_for("max"), "muse-wonder-1.0")
        p, _ = self.make([], private=True)
        self.assertEqual(p.model_for("draft"), "muse-spark-1.3")
        with self.assertRaises(ValueError):
            m.model_for("cheap")

    def test_second_identical_call_is_free_cache_hit(self):
        m, t = self.make([fake_response()])
        msgs = [{"role": "user", "content": "hola"}]
        m.chat(msgs, tag="t")
        m.chat(msgs, tag="t")
        self.assertEqual(len(t.calls), 1)
        rows = m.ledger_path.read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["model"], "muse-spark-1.3-contributor")

    def test_replay_mode_never_hits_network(self):
        m, t = self.make([], mode="replay")
        with self.assertRaises(ReplayMiss):
            m.chat([{"role": "user", "content": "x"}])
        self.assertEqual(t.calls, [])

    def test_budget_hard_stop(self):
        m, t = self.make([fake_response()], hard_stop_usd=0.5)
        m.record(model="muse-spark-1.3", cost_usd=0.6, tag="seed")
        with self.assertRaises(BudgetExceeded):
            m.chat([{"role": "user", "content": "y"}])
        self.assertEqual(t.calls, [])

    def test_retries_on_429_then_succeeds(self):
        m, t = self.make([ApiError(429, "slow down"), ApiError(503, "busy"), fake_response()])
        m.chat([{"role": "user", "content": "z"}])
        self.assertEqual(len(t.calls), 3)

    def test_non_retryable_error_raises(self):
        m, _ = self.make([ApiError(400, "bad")])
        with self.assertRaises(ApiError):
            m.chat([{"role": "user", "content": "w"}])

    def test_json_schema_payload_and_parse(self):
        m, t = self.make([fake_response(content='{"rooms": []}')])
        out = m.chat_json([{"role": "user", "content": "plan"}], json_schema={"type": "object"}, tier="standard")
        self.assertEqual(out, {"rooms": []})
        payload = t.calls[0][1]
        self.assertEqual(payload["model"], "muse-spark-1.3")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(t.calls[0][0].endswith("/chat/completions"))

    def test_json_truncated_by_reasoning_raises_clear_error(self):
        # Spark reasons before answering: a small max_tokens can be spent entirely on reasoning.
        r = fake_response(out=200, content=None)
        r["choices"][0]["finish_reason"] = "length"
        r["usage"]["completion_tokens_details"] = {"reasoning_tokens": 198}
        m, _ = self.make([r])
        with self.assertRaises(EmptyResponse) as ctx:
            m.chat_json([{"role": "user", "content": "plan"}], json_schema={"type": "object"}, max_tokens=200)
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertIn("198", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
