"""Harness selection: policies, fallback chain, exports (no secrets, admin secrets stripped), logs, handoff."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mme import cli


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"MME_PROJECT": str(self.tmp), "MODEL_API_KEY": "secret-mk",
                                                "MME_GATEWAY_KEY": "secret-gw"})
        self.env.start()
        for k in ("MME_POLICY", "MME_HARNESS", "MME_PRIVATE", "LITELLM_MASTER_KEY"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_default_policy_is_meta_first(self):
        from mme import harness
        self.assertEqual(harness.read_policy(), "meta-first")

    def test_meta_only_refuses_claude(self):
        self.run_cli("policy", "meta-only")
        code, out, err = self.run_cli("use", "claude")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("NOT allowed", err)

    def test_meta_first_allows_claude_and_logs_it(self):
        code, out, _ = self.run_cli("use", "claude", "gateway", "down")
        self.assertEqual(code, 0)
        self.assertIn('export MME_HARNESS="claude"', out)
        log = (self.tmp / "docs/AI_USAGE.md").read_text()
        self.assertIn("| interactive | claude | anthropic |", log)
        self.assertIn("gateway down", log)

    def test_gateway_exports_reference_vars_and_strip_admin_secrets(self):
        _, out, _ = self.run_cli("use", "gateway")
        self.assertIn('export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"', out)
        self.assertIn('export ANTHROPIC_AUTH_TOKEN="$MME_GATEWAY_KEY"', out)
        self.assertIn('export ANTHROPIC_DEFAULT_OPUS_MODEL="muse-spark-1.3-contributor"', out)
        unset = out.splitlines()[0]
        for secret in ("LITELLM_MASTER_KEY", "MME_NIGHT_KEY", "POSTGRES_PASSWORD", "MODEL_API_KEY"):
            self.assertIn(secret, unset)
        self.assertNotIn("secret-", out)

    def test_meta_direct_keeps_model_key(self):
        _, out, _ = self.run_cli("use", "meta-direct")
        self.assertNotIn(" MODEL_API_KEY", out.splitlines()[0])
        self.assertIn('ANTHROPIC_AUTH_TOKEN="$MODEL_API_KEY"', out)

    def test_private_mode_uses_standard_spark(self):
        os.environ["MME_PRIVATE"] = "1"
        _, out, _ = self.run_cli("use", "meta-direct")
        self.assertIn('ANTHROPIC_MODEL="muse-spark-1.3"', out)

    def test_missing_key_refused(self):
        del os.environ["MME_GATEWAY_KEY"]
        code, out, _ = self.run_cli("use", "gateway")
        self.assertEqual((code, out), (4, ""))

    def test_fallback_chains(self):
        os.environ["MME_HARNESS"] = "gateway"
        self.assertIn('MME_HARNESS="meta-direct"', self.run_cli("fallback", "down")[1])
        os.environ["MME_HARNESS"] = "muse-code"
        self.assertIn('MME_HARNESS="claude"', self.run_cli("fallback")[1])
        self.run_cli("policy", "meta-only")
        seen, cur = [], "gateway"
        for _ in range(6):
            os.environ["MME_HARNESS"] = cur
            cur = self.run_cli("fallback")[1].split('MME_HARNESS="')[1].split('"')[0]
            seen.append(cur)
        self.assertNotIn("claude", seen)

    def test_switch_writes_handoff(self):
        os.environ["MME_HARNESS"] = "gateway"
        self.run_cli("use", "muse-code", "trying Meta's agent")
        self.assertIn("gateway -> muse-code", (self.tmp / "HANDOFF.md").read_text())

    def test_policy_change_warns(self):
        os.environ["MME_HARNESS"] = "claude"
        self.assertIn("no longer allowed", self.run_cli("policy", "meta-only")[2])

    def test_shell_function_end_to_end(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "MME_HARNESS"))}
        env.update(MME_GATEWAY_KEY="gw", MODEL_API_KEY="mk", LITELLM_MASTER_KEY="", MME_PROJECT=str(self.tmp))
        script = (f'mme() {{ case "$1" in use|fallback) local o; o="$({sys.executable} -m mme "$@")" || return $?; '
                  f'eval "$o";; *) {sys.executable} -m mme "$@";; esac; }}; '
                  'export LITELLM_MASTER_KEY=sk-admin; mme use gateway >/dev/null 2>&1; '
                  'echo "$ANTHROPIC_BASE_URL|$ANTHROPIC_AUTH_TOKEN|$MME_HARNESS|${LITELLM_MASTER_KEY:-stripped}"')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(r.stdout.strip(), "http://127.0.0.1:4000|gw|gateway|stripped")

    def test_shell_init_prints_function(self):
        self.assertIn("eval \"$out\"", self.run_cli("shell-init")[1])


if __name__ == "__main__":
    unittest.main()
