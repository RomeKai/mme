"""Run POSIX shell command strings (gates, mission checks) the same way on every OS.

`subprocess.run(cmd, shell=True)` means cmd.exe on Windows, which can't run `VAR=x cmd`, `test`, `$(...)` or quoting
that the gate commands rely on. On Windows, run them through bash (Git Bash) instead.
"""
from __future__ import annotations

import os


def shell_args(cmd: str) -> tuple[list[str] | str, bool]:
    """Return (args, shell) for subprocess so `cmd` is interpreted by a POSIX shell."""
    if os.name == "nt":
        from mme import cli  # lazy: cli imports most modules

        bash = cli.find_bash()
        if bash:
            return [bash, "-c", cmd], False
    return cmd, True
