"""Night missions end to end with a fake agent and a throwaway git repo:
templates + cheap review, briefing (prep), staged run, repair, review read-only, scope, budgets, report."""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mme.night import missions as ms

FAKE_AGENT = textwrap.dedent('''
    import json, os, pathlib, sys, time
    kind, stage, mode = os.environ["MME_STAGE_KIND"], os.environ["MME_STAGE"], os.environ.get("FAKE_MODE", "ok")
    log = pathlib.Path(os.environ["FAKE_LOG"]); log.open("a").write(stage + "\\n")
    m = pathlib.Path(".mission")
    if kind == "prep":
        print(json.dumps({"result": "Sure. " + json.dumps({"questions": [
            {"question": "Meters or millimetres?", "why": "units in DXF", "default": "meters"},
            {"question": "Max rooms?", "why": "test size", "default": "12"}],
            "risks": ["concave sites"], "budget_ok": True})}))
        sys.exit(0)
    if mode == "slow":
        time.sleep(4)
    if kind == "plan":
        (m / "PLAN.md").write_text("plan: do it")
    elif kind == "build":
        if mode == "blocked":
            (m / "BLOCKED.md").write_text("need a decision on units")
        elif mode == "fail_then_repair" and not stage.endswith("-repair"):
            pass
        elif mode == "always_fail":
            pass
        else:
            pathlib.Path("done.txt").write_text("done")
            if mode == "out_of_scope":
                pathlib.Path("other.txt").write_text("oops")
    elif kind == "review":
        (m / "REVIEW.md").write_text("minor: naming\\nrecommend: merge")
        if mode == "naughty_review":
            pathlib.Path("done.txt").write_text("review rewrote code")
    print("fake", stage, "ok")
''')


class MissionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        (self.repo / "mme" / "night").mkdir(parents=True)
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (self.repo / "README.md").write_text("x")
        (self.repo / "AGENTS.md").write_text("rules")
        (self.repo / "mme" / "budget.json").write_text(json.dumps({
            "start": "2026-10-26", "end": "2026-11-04", "total_harness_usd": 60, "weekday_usd": 5, "weekend_usd": 2.5,
            "rollover": True, "day_max_usd": 12, "night": {"pool_usd": 15, "max_per_run_usd": 6.0, "review_max_usd": 0.3}}))
        self.write_missions("m1")
        g("add", "-A")
        g("commit", "-qm", "init")
        agent = self.tmp / "fake_agent.py"
        agent.write_text(FAKE_AGENT)
        self.log = self.tmp / "agent.log"
        self.env = mock.patch.dict(os.environ, {
            "MME_PROJECT": str(self.repo), "MME_NIGHT_AGENT_CMD": f"{sys.executable} {agent}",
            "MME_NIGHT_KEY": "nk", "MME_NIGHT_POLL_S": "0.1", "FAKE_LOG": str(self.log), "FAKE_MODE": "ok"})
        self.env.start()
        os.environ.pop("LITELLM_MASTER_KEY", None)
        os.environ.pop("MME_GATEWAY_KEY", None)

    def tearDown(self):
        self.env.stop()

    def write_missions(self, *ids, template="feature", budget=2.0, extra=""):
        body = "defaults:\n  base: main\nmissions:\n"
        for i in ids:
            body += (f"  - id: {i}\n    template: {template}\n    budget_usd: {budget}\n    goal: build {i}\n"
                     f"    context: [AGENTS.md]\n    scope: [done.txt]\n    verify: test -f done.txt\n{extra}")
        (self.repo / "mme" / "night" / "missions.yaml").write_text(body)

    def args(self, **kw):
        return argparse.Namespace(until=kw.get("until"), only=kw.get("only"), dry_run=kw.get("dry_run", False))

    def prep_yes(self):
        return ms.prep(ms.load_missions(), yes=True, spend=lambda: 0.0)

    def results(self):
        return json.loads((self.repo / ".mme/night/state.json").read_text())["results"]

    # ---------------- templates & budgets
    def test_feature_template_review_is_capped_and_surplus_goes_to_build(self):
        m = ms.load_missions()[0]
        st = {s.name: s.budget_usd for s in m.stages}
        self.assertEqual(list(st), ["plan", "build", "harden", "review"])
        self.assertEqual(st["review"], 0.20)                       # 10% of $2 is under the $0.30 cap
        self.assertAlmostEqual(sum(st.values()), 2.0)

    def test_review_cap_applies_on_big_missions(self):
        self.write_missions("big", budget=10.0)
        m = ms.load_missions()[0]
        st = {s.name: s.budget_usd for s in m.stages}
        self.assertEqual(st["review"], 0.30)
        self.assertAlmostEqual(st["build"], 6.0 + 0.7)            # 10% review ($1) -> $0.30, $0.70 to build
        self.assertAlmostEqual(sum(st.values()), 10.0)

    def test_unknown_template_rejected(self):
        self.write_missions("x", template="nope")
        with self.assertRaises(ValueError):
            ms.load_missions()

    # ---------------- briefing
    def test_run_refuses_unbriefed_missions(self):
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertFalse((self.repo / ".mme/night/state.json").exists())

    def test_prep_asks_questions_and_stores_answers(self):
        answers = iter(["millimetres", "", "keep it simple"])
        code = ms.prep(ms.load_missions(), ask=lambda _: next(answers), spend=lambda: 0.0)
        self.assertEqual(code, 1)   # preflight fails: gateway not reachable in tests (expected)
        b = json.loads((self.repo / ".mme/night/briefs/m1.json").read_text())
        self.assertEqual([a["answer"] for a in b["answers"]], ["millimetres", "12"])
        self.assertEqual(b["notes"], "keep it simple")
        self.assertIn("Meters or millimetres? -> millimetres", ms.decisions_text(ms.load_missions()[0]))

    def test_editing_a_mission_invalidates_its_brief(self):
        self.prep_yes()
        self.assertTrue(ms.is_ready(ms.load_missions()[0]))
        self.write_missions("m1", budget=3.0)
        self.assertFalse(ms.is_ready(ms.load_missions()[0]))

    # ---------------- run
    def test_full_feature_mission_passes_on_its_own_branch(self):
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        r = self.results()["m1"]
        self.assertEqual(r["result"], "PASS")
        self.assertEqual([s["stage"] for s in r["stages"]], ["plan", "build", "harden", "review"])
        self.assertFalse((self.repo / "done.txt").exists())          # main untouched
        log = subprocess.run(["git", "log", "--oneline", f"main..{r['branch']}"], cwd=self.repo,
                             capture_output=True, text=True).stdout
        self.assertIn("night(m1:", log)
        report = (self.repo / "NIGHT_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("**PASS**", report)
        self.assertIn("recommend: merge", report)
        self.assertIn("night", (self.repo / "docs/AI_USAGE.md").read_text())

    def test_decisions_reach_the_agent_prompt(self):
        self.prep_yes()
        captured = []
        real = ms.agent_cmd
        with mock.patch.object(ms, "agent_cmd", side_effect=lambda m, p, t, readonly: captured.append(p) or real(m, p, t, readonly)):
            ms.run(self.args(), spend=lambda: 0.0)
        self.assertIn("Meters or millimetres? -> meters", captured[0])

    def test_failed_check_gets_one_repair(self):
        os.environ["FAKE_MODE"] = "fail_then_repair"
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(self.results()["m1"]["result"], "PASS")
        self.assertIn("build-repair", self.log.read_text())

    def test_always_failing_build_stops_mission(self):
        os.environ["FAKE_MODE"] = "always_fail"
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        r = self.results()["m1"]
        self.assertEqual(r["result"], "FAIL")
        self.assertEqual([s["stage"] for s in r["stages"]], ["plan", "build"])

    def test_blocked(self):
        os.environ["FAKE_MODE"] = "blocked"
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(self.results()["m1"]["result"], "BLOCKED")
        self.assertIn("need a decision on units", (self.repo / "NIGHT_REPORT.md").read_text(encoding="utf-8"))

    def test_review_stage_is_read_only(self):
        os.environ["FAKE_MODE"] = "naughty_review"
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        r = self.results()["m1"]
        review = next(s for s in r["stages"] if s["stage"] == "review")
        self.assertEqual(review["reverted"], ["done.txt"])
        content = subprocess.run(["git", "show", f"{r['branch']}:done.txt"], cwd=self.repo,
                                 capture_output=True, text=True).stdout
        self.assertEqual(content, "done")

    def test_out_of_scope_files_are_flagged(self):
        os.environ["FAKE_MODE"] = "out_of_scope"
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(self.results()["m1"]["out_of_scope"], ["other.txt"])
        self.assertIn("Out of scope", (self.repo / "NIGHT_REPORT.md").read_text(encoding="utf-8"))

    def test_stage_killed_when_over_its_budget(self):
        os.environ["FAKE_MODE"] = "slow"
        self.prep_yes()
        n = [0.0]

        def spend():
            n[0] += 0.15
            return n[0]
        ms.run(self.args(), spend=spend)
        r = self.results()["m1"]
        self.assertEqual(r["stages"][0]["result"], "OVER_BUDGET")   # plan stage budget is $0.20

    def test_per_night_cap_skips_missions(self):
        self.write_missions("a", "b", "c", "d", budget=2.0)   # cap $6 -> 3 fit
        subprocess.run(["git", "commit", "-qam", "missions"], cwd=self.repo, capture_output=True)
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(sorted(self.results()), ["a", "b", "c"])
        self.assertIn("per-night cap", (self.repo / "NIGHT_REPORT.md").read_text(encoding="utf-8"))

    def test_passed_mission_not_rerun_unless_edited(self):
        self.prep_yes()
        ms.run(self.args(), spend=lambda: 0.0)
        before = self.log.read_text().count("\n")
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(self.log.read_text().count("\n"), before)

    def test_dry_run_shows_stage_budgets(self):
        self.prep_yes()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ms.run(self.args(dry_run=True))
        self.assertIn("plan $0.20 -> build $1.20 -> harden $0.40 -> review $0.20", buf.getvalue())

    def test_claude_harness_runs_when_policy_allows(self):
        self.write_missions("c1", template="single", extra="    harness: claude\n")
        subprocess.run(["git", "commit", "-qam", "claude mission"], cwd=self.repo, capture_output=True)
        self.prep_yes()
        os.environ.pop("MME_NIGHT_KEY")
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertEqual(self.results()["c1"]["result"], "PASS")
        env = ms.agent_env(ms.load_missions()[0], "MME_NIGHT_KEY", "build", "do")
        self.assertFalse(any(k.startswith("ANTHROPIC_") for k in env))

    def test_claude_harness_refused_under_meta_only(self):
        (self.repo / "mme" / "policy.env").write_text("MME_POLICY=meta-only\n")
        self.write_missions("c2", template="single", extra="    harness: claude\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo)
        subprocess.run(["git", "commit", "-qm", "policy"], cwd=self.repo, capture_output=True)
        self.prep_yes()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ms.run(self.args(dry_run=True))
        self.assertIn("not allowed by policy 'meta-only'", buf.getvalue())
        ms.run(self.args(), spend=lambda: 0.0)
        self.assertFalse((self.repo / ".mme/night/state.json").exists())

    def test_parse_json_reply_variants(self):
        self.assertEqual(ms.parse_json_reply('{"questions": []}'), {"questions": []})
        self.assertEqual(ms.parse_json_reply(json.dumps({"result": 'ok {"questions": [1]} bye'}))["questions"], [1])
        with self.assertRaises(ValueError):
            ms.parse_json_reply("no json here")

    def test_parse_json_reply_skips_noise_before_the_envelope(self):
        inner = 'Sure. {"questions": [{"question": "q", "default": "d"}], "risks": ["see {braces}"]}'
        out = "warning: hook {x} failed\n" + json.dumps({"type": "result", "result": inner})
        self.assertEqual(ms.parse_json_reply(out)["questions"][0]["question"], "q")

    def test_parse_json_reply_never_reads_the_envelope_as_no_questions(self):
        with self.assertRaises(ValueError):
            ms.parse_json_reply("note\n" + json.dumps({"type": "result", "result": "I have no JSON for you"}))


class ClaudeBinTest(unittest.TestCase):
    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"MME_CLAUDE_BIN": "/opt/claude"}):
            self.assertEqual(ms.claude_bin(), "/opt/claude")

    def test_npm_cmd_shim_resolves_to_bundled_exe(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            exe = Path(d) / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            env = {k: v for k, v in os.environ.items() if k != "MME_CLAUDE_BIN"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ms.shutil, "which", return_value=str(shim)):
                self.assertEqual(ms.claude_bin(), str(exe))

    def test_agent_cmd_uses_resolved_binary(self):
        m = ms.Mission(id="x", goal="g", verify="true", harness="gateway")
        with mock.patch.object(ms, "claude_bin", return_value="/bin/claude-real"):
            self.assertEqual(ms.agent_cmd(m, "p", 3, readonly=True)[0], "/bin/claude-real")


if __name__ == "__main__":
    unittest.main()
