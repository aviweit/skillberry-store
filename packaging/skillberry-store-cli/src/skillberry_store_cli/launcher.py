# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Hand the process over to the bundled `sbs` binary.

Three lines of real work, and one deliberate choice: on POSIX this ``execv``s
rather than spawning a subprocess, so the binary *replaces* this process. That
matters for a CLI:

* signals (Ctrl-C, SIGTERM) reach the program the user is actually running,
  rather than a Python parent that may or may not forward them;
* the exit status is the binary's own, with no translation layer to get wrong;
* there is no extra process in ``ps``, no doubled memory, and no risk of an
  orphan if the parent is killed;
* the TTY belongs to the binary, so colour, progress and interactive prompts
  behave exactly as they do for a directly-installed binary.

Windows has no ``execv`` that replaces the image in a usable way (its emulation
returns immediately and detaches, which breaks piping and exit codes), so there
the binary is spawned and its status is propagated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def binary_path() -> Path:
    """Where the wheel put the executable."""
    name = "sbs.exe" if sys.platform == "win32" else "sbs"
    return Path(__file__).parent / "bin" / name


def main() -> int:
    """Entry point for the ``sbs`` console script."""
    target = binary_path()

    if not target.is_file():
        # A wheel built for the wrong platform, or an sdist install (which
        # carries no binary at all). Say what to do instead of failing with a
        # bare FileNotFoundError from execv.
        sys.stderr.write(
            f"sbs: the bundled executable is missing from {target}.\n"
            "\n"
            "This usually means skillberry-store-cli was installed from a source\n"
            "distribution, or from a wheel built for another platform. Install a\n"
            "platform wheel, or get the binary straight from your store:\n"
            "\n"
            "  pip install --force-reinstall --only-binary :all: skillberry-store-cli\n"
            '  curl -fsSL "<store-url>/cli/download?platform=linux-amd64" -o sbs\n'
        )
        return 1

    # Wheels do not reliably preserve the executable bit across every build and
    # install path, so ensure it rather than assuming it. Best effort: a
    # read-only site-packages is not worth failing over, and execv will report
    # the real problem if the bit is genuinely missing.
    if os.name != "nt" and not os.access(target, os.X_OK):
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass

    argv = [str(target), *sys.argv[1:]]

    if os.name == "nt":
        import subprocess

        return subprocess.call(argv)

    # Replaces this process; never returns on success.
    os.execv(str(target), argv)
    return 1  # pragma: no cover - unreachable unless execv fails


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
