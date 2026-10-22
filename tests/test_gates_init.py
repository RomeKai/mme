"""Delivery gates (today/gate/ship) and `mme init` (idempotent install into a project)."""
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mme import cli, gates, init


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)


class GatesTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "mme").mkdir()
        today = dt.date.today()
        self.cfg = {
            "deadline": (dt.datetime.now().astimezone() + dt.timedelta(days=3)).isoformat(),
            "internal_deadline": (dt.datetime.now().astimezone() + dt.timedelta(days=2, hours=5)).isoformat(),
            "freeze": (today + dt.timedelta(days=1)).isoformat(),
            "test_cmd": "true",
            "milestones": [
                {"day": (today - dt.timedelta(days=1)).isoformat(), "goal": "yesterday thing",
                 "gates": [{"name": "y-ok", "run": "true"}, {"name": "y-bad", "run": "test -f nope"}],
                 "cut_if_late": ["voice intake"]},
                {"day": today.isoformat(), "goal": "today thing", "gates": [{"name": "t-ok", "run": "true"}]},
            ],
            "ship": {"replay_dir": "samples/replay", "deploy_url": ""},
        }
        import yaml
        (self.repo / "mme" / "milestones.yaml").write_text(yaml.safe_dump(self.cfg))
        self.env = mock.patch.dict(os.environ, {"MME_PROJECT": str(self.repo)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_unrun_past_gates_count_as_behind(self):
        behind, todays = gates.gate_status(gates.load(), dt.date.today())
        self.assertEqual(len(behind), 1)
        self.assertEqual(todays[0]["goal"], "today thing")

    def test_gate_runs_today_and_late_and_board_shows_cut_suggestions(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["gate"])
        self.assertEqual(code, 1)  # y-bad fails
        board = gates.today_board(gates.load())
        self.assertIn("BEHIND", board)
        self.assertIn("y-bad", board)
        self.assertIn("consider cutting: voice intake", board)
        self.assertIn("to the internal deadline", board)
        self.assertIn("2d", gates.short_line(gates.load()))

    def test_on_track_when_past_gates_green(self):
        import yaml
        self.cfg["milestones"][0]["gates"] = [{"name": "y-ok", "run": "true"}]
        (self.repo / "mme" / "milestones.yaml").write_text(yaml.safe_dump(self.cfg))
        with redirect_stdout(io.StringIO()):
            cli.main(["gate"])
        self.assertIn("On track", gates.today_board(gates.load()))

    def test_freeze_banner(self):
        later = dt.datetime.now().astimezone() + dt.timedelta(days=1, hours=1)
        self.assertIn("FEATURE FREEZE", gates.today_board(gates.load(), now=later))

    def test_ship_flags_missing_items_and_secrets(self):
        (self.repo / "README.md").write_text("# App\nUses Muse Spark.\n```\nrun it\n```\nDemo: https://youtu.be/x\n")
        (self.repo / "config.py").write_text('KEY = "sk-' + "a" * 24 + '"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "x")
        checks = {name: ok for name, ok, _ in gates.ship_checks(gates.load())}
        self.assertTrue(checks["README names the Meta models used"])
        self.assertTrue(checks["README has a demo video link"])
        self.assertFalse(checks["no secrets in tracked files"])
        self.assertFalse(checks["LICENSE present"])
        self.assertFalse(checks["deploy URL set in milestones.yaml (ship.deploy_url)"])


class InitTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q", "-b", "main")
        self.env = mock.patch.dict(os.environ, {"MME_PROJECT": str(self.repo)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_init_creates_files_and_is_idempotent(self):
        (self.repo / "AGENTS.md").write_text("# My project\nmy rules\n")
        report = dict(init.init(self.repo, start=dt.date(2026, 10, 26)))
        self.assertEqual(report["mme/budget.json"], "created")
        self.assertEqual(report[".claude/settings.json"], "created")
        self.assertEqual(report[".env.example"], "created")
        plan = json.loads((self.repo / "mme/budget.json").read_text())
        self.assertEqual((plan["start"], plan["end"]), ("2026-10-26", "2026-11-04"))
        agents = (self.repo / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith("# My project"))
        self.assertIn("mme:start", agents)
        self.assertIn("@AGENTS.md", (self.repo / "CLAUDE.md").read_text())
        skill = (self.repo / ".claude/skills/meta-model-api/SKILL.md").read_text()
        self.assertIn("muse-wonder-1.0", skill)        # table rendered from provider catalog
        self.assertNotIn("{{", skill)
        settings = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(settings["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"], "mme hook loop-breaker")
        self.assertIn("Bash(*/key/*)", settings["permissions"]["deny"])
        # second run: nothing clobbered, block not duplicated
        (self.repo / "mme/budget.json").write_text('{"mine": true}')
        report2 = dict(init.init(self.repo))
        self.assertEqual(report2["mme/budget.json"], "skipped (exists)")
        self.assertEqual((self.repo / "AGENTS.md").read_text().count("mme:start"), 1)
        self.assertEqual((self.repo / ".gitignore").read_text().count("# mme:start"), 1)

    def test_doctor_runs(self):
        init.init(self.repo)
        with redirect_stdout(io.StringIO()) as buf:
            cli.main(["doctor"])
        self.assertIn("budget plan valid", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
