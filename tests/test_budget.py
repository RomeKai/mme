"""Variable budget planner: weekday/weekend/overrides, rollover with a daily ceiling, prelaunch, sync."""
import datetime as dt
import unittest

from mme import budget

D = dt.date.fromisoformat
PLAN = {
    "start": "2026-10-26", "end": "2026-11-04", "total_harness_usd": 60,
    "weekday_usd": 5.0, "weekend_usd": 2.5, "overrides": {"2026-11-04": 3.0},
    "rollover": True, "day_max_usd": 12.0, "prelaunch_usd": 1.0,
    "night": {"pool_usd": 15.0, "max_per_run_usd": 4.0, "review_max_usd": 0.30},
}


class BudgetTest(unittest.TestCase):
    def test_plan_is_valid(self):
        self.assertEqual(budget.validate(PLAN), [])
        self.assertEqual(budget.day_pool(PLAN), 43.0)

    def test_allowances(self):
        self.assertEqual(budget.allowance(PLAN, D("2026-10-26")), 5.0)   # Mon
        self.assertEqual(budget.allowance(PLAN, D("2026-10-31")), 2.5)   # Sat
        self.assertEqual(budget.allowance(PLAN, D("2026-11-04")), 3.0)   # override
        self.assertEqual(budget.allowance(PLAN, D("2026-11-05")), 0.0)   # after end

    def test_rollover_and_daily_ceiling(self):
        self.assertEqual(budget.cap_for(PLAN, D("2026-10-31"), 8.0), 20.0)    # ceiling 8 + 12
        self.assertEqual(budget.cap_for(PLAN, D("2026-10-31"), 20.0), 27.5)   # cumulative
        self.assertEqual(budget.cap_for(PLAN, D("2026-10-27"), 5.0), 10.0)

    def test_prelaunch_allowance(self):
        self.assertEqual(budget.cap_for(PLAN, D("2026-10-24"), 0.3), 1.3)

    def test_no_rollover(self):
        self.assertEqual(budget.cap_for({**PLAN, "rollover": False}, D("2026-10-31"), 3.0), 5.5)

    def test_snapshot_once_per_day(self):
        a, st = budget.snapshot({}, D("2026-10-27"), 4.0)
        b, _ = budget.snapshot(st, D("2026-10-27"), 6.0)
        self.assertEqual((a, b), (4.0, 4.0))

    def test_sync_pushes_cap(self):
        calls = []

        def http(method, path, body):
            calls.append((method, path, body))
            return {"info": {"spend": 7.0}} if method == "GET" else {}
        spent, cap, _ = budget.sync(http, "k", PLAN, D("2026-10-28"), {})
        self.assertEqual((spent, cap), (7.0, 15.0))
        self.assertEqual(calls[-1], ("POST", "/key/update", {"key": "k", "max_budget": 15.0}))

    def test_night_settings_and_legacy_keys(self):
        self.assertEqual(budget.night_settings(PLAN)["review_max_usd"], 0.30)
        legacy = {k: v for k, v in PLAN.items() if k != "night"} | {"night_pool_usd": 9.0}
        self.assertEqual(budget.night_settings(legacy)["pool_usd"], 9.0)

    def test_invalid_plan_detected(self):
        self.assertTrue(budget.validate({**PLAN, "weekday_usd": 9.0}))


if __name__ == "__main__":
    unittest.main()
