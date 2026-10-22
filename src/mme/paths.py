"""Where things live. MME code is a package; everything stateful belongs to the PROJECT that uses it.

  project root   MME_PROJECT, else the enclosing git repo, else cwd
  <root>/mme/    committed config: budget.json, policy.env, milestones.yaml, night/missions.yaml
  <root>/.mme/   local state (gitignored): ledger, caches, gateway config, night state, gate results
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def project_root() -> Path:
    env = os.environ.get("MME_PROJECT")
    if env:
        return Path(env).resolve()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


def config_dir() -> Path:
    return project_root() / "mme"


def state_dir() -> Path:
    return Path(os.environ["MME_HOME"]).resolve() if os.environ.get("MME_HOME") else project_root() / ".mme"
