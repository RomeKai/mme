"""Guards: loop breaker, burst watchdog, gateway config built from the provider catalog."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mme.hooks import loop_breaker as lb  # noqa: E402
from mme import watchdog as wd  # noqa: E402
from mme.gateway import render as rc  # noqa: E402


class LoopBreakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def ev(self, name, cmd="python -m pytest -q", tool="Bash"):
        return {"hook_event_name": name, "session_id": "s1", "cwd": self.tmp.name,
                "tool_name": tool, "tool_input": {"command": cmd}}

    def test_signature_normalizes_variants(self):
        self.assertEqual(lb.signature("cd server && MME_MODE=replay python -m pytest -q tests/a.py"), "pytest")
        self.assertEqual(lb.signature("pytest -x"), "pytest")
        self.assertEqual(lb.signature("npm run build -- --force"), "npm build")
        self.assertEqual(lb.signature("npx tsc --noEmit"), "tsc")
        self.assertEqual(lb.signature("cd server && pytest -q 2>&1 | tail -20"), "pytest")

    def test_warn_then_stop(self):
        self.assertIsNone(lb.handle(self.ev("PostToolUseFailure")))
        warn = lb.handle(self.ev("PostToolUseFailure", "cd server && pytest -q -x"))
        self.assertIn("additionalContext", warn["hookSpecificOutput"])
        stop = lb.handle(self.ev("PostToolUseFailure"))
        self.assertIs(stop["continue"], False)
        self.assertIn("pytest", stop["stopReason"])

    def test_success_resets_signature(self):
        lb.handle(self.ev("PostToolUseFailure"))
        lb.handle(self.ev("PostToolUseFailure"))
        lb.handle(self.ev("PostToolUse"))
        self.assertIsNone(lb.handle(self.ev("PostToolUseFailure")))

    def test_human_message_resets_everything(self):
        lb.handle(self.ev("PostToolUseFailure"))
        lb.handle(self.ev("PostToolUseFailure"))
        lb.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": self.tmp.name})
        self.assertIsNone(lb.handle(self.ev("PostToolUseFailure")))

    def test_session_wide_cap_across_different_commands(self):
        cmds = ["pytest", "ruff check", "tsc", "mypy .", "pytest", "ruff check", "tsc"]  # 7 failures, none 3x
        for c in cmds:
            self.assertNotEqual((lb.handle(self.ev("PostToolUseFailure", c)) or {}).get("continue"), False)
        out = lb.handle(self.ev("PostToolUseFailure", "eslint ."))
        self.assertIs(out["continue"], False)

    def test_non_bash_tools_ignored(self):
        self.assertIsNone(lb.handle(self.ev("PostToolUseFailure", tool="Edit")))

    def test_cli_never_crashes_on_garbage(self):
        p = subprocess.run([sys.executable, "-m", "mme", "hook", "loop-breaker"],
                           input="not json", text=True, capture_output=True)
        self.assertEqual(p.returncode, 0)


class WatchdogTest(unittest.TestCase):
    def test_detects_burst_inside_window(self):
        d = wd.Watchdog(threshold_usd=1.0, window_s=600)
        self.assertIsNone(d.observe(0, 0.10))
        self.assertIsNone(d.observe(300, 0.60))
        self.assertAlmostEqual(d.observe(500, 1.25), 1.15)

    def test_slow_steady_spend_is_fine(self):
        d = wd.Watchdog(threshold_usd=1.0, window_s=600)
        for i in range(0, 7200, 60):  # $0.05/min for 2h = $6 total but never $1 in 10 min
            self.assertIsNone(d.observe(i, i / 60 * 0.05 * 0.3))

    def test_budget_reset_starts_over(self):
        d = wd.Watchdog(threshold_usd=1.0, window_s=600)
        d.observe(0, 5.0)
        self.assertIsNone(d.observe(60, 0.2))

    def test_key_spend_parses_info(self):
        self.assertEqual(wd.key_spend(lambda m, p, b: {"info": {"spend": 2.5}}, "k"), 2.5)

    def test_key_spend_prefers_live_window_counter(self):
        # DB spend lags (batched writes); the budget-window counter is the one enforcement reads.
        info = {"info": {"spend": 0.0, "budget_limits_usage": {"1d": {"current_spend": 0.69}}}}
        self.assertEqual(wd.key_spend(lambda m, p, b: info, "k"), 0.69)

    def test_key_spend_uses_highest_window_counter(self):
        info = {"info": {"spend": 0.0, "budget_limits_usage": {"1h": {"current_spend": 0.1},
                                                               "1d": {"current_spend": 0.4}}}}
        self.assertEqual(wd.key_spend(lambda m, p, b: info, "k"), 0.4)

    def test_key_spend_never_below_db_spend(self):
        # After a LiteLLM restart the live counter can read lower than the persisted DB spend.
        info = {"info": {"spend": 0.9, "budget_limits_usage": {"1d": {"current_spend": 0.0}}}}
        self.assertEqual(wd.key_spend(lambda m, p, b: info, "k"), 0.9)

    def test_key_spend_ignores_malformed_windows(self):
        for bad in ([{"current_spend": 1}], {"1d": {"current_spend": "n/a"}}, {"1d": None}):
            info = {"info": {"spend": 0.3, "budget_limits_usage": bad}}
            self.assertEqual(wd.key_spend(lambda m, p, b: info, "k"), 0.3)

    def test_key_spend_falls_back_to_db_without_windows(self):
        info = {"info": {"spend": 1.2, "budget_limits_usage": None}}
        self.assertEqual(wd.key_spend(lambda m, p, b: info, "k"), 1.2)


class RenderConfigTest(unittest.TestCase):
    ENV = {"MODEL_API_KEY": "mk", "LITELLM_MASTER_KEY": "sk-x", "POSTGRES_PASSWORD": "pw"}

    def test_backend_a_and_b_from_provider_catalog(self):
        a = rc.build(self.ENV)
        b = rc.build(self.ENV, openai=True)
        first = a["model_list"][0]["litellm_params"]
        self.assertEqual(first["model"], "anthropic/muse-spark-1.3-contributor")
        self.assertEqual(first["api_base"], "https://api.meta.ai")
        self.assertEqual(first["extra_headers"]["Authorization"], "Bearer mk")
        self.assertEqual(b["model_list"][0]["litellm_params"]["api_base"], "https://api.meta.ai/v1")
        self.assertEqual(a["litellm_settings"]["max_budget"], 60.0)
        self.assertEqual(a["general_settings"]["database_url"], "postgresql://litellm:pw@db:5432/litellm")
        wonder = next(m for m in a["model_list"] if m["model_name"] == "muse-wonder-1.0")
        self.assertAlmostEqual(wonder["model_info"]["output_cost_per_token"] * 1e6, 20.0)
        self.assertEqual(a["router_settings"]["fallbacks"], [{"muse-spark-1.3-contributor": ["muse-spark-1.3"]}])

    def test_prices_come_from_one_place(self):
        from mme import providers
        cfg = rc.build(self.ENV)
        for m in cfg["model_list"]:
            p = providers.load()["text_models"][m["model_name"]]
            self.assertAlmostEqual(m["model_info"]["input_cost_per_token"] * 1e6, p["input"])

    def test_mock_upstream(self):
        c = rc.build({"LITELLM_MASTER_KEY": "sk-x", "POSTGRES_PASSWORD": "pw"}, mock=True)
        p = c["model_list"][0]["litellm_params"]
        self.assertEqual(p["api_base"], "http://meta-mock:8090")
        self.assertEqual(p["api_key"], "mock-key")

    def test_missing_key_fails(self):
        with self.assertRaises(KeyError):
            rc.build({"LITELLM_MASTER_KEY": "sk-x"})


if __name__ == "__main__":
    unittest.main()
