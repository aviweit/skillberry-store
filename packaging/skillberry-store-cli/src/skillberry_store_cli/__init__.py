# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Launcher package for the native `sbs` CLI.

This package exists only to put the Go binary on a `pip` user's PATH. It is the
pattern `ruff`, `uv` and `esbuild` use: a platform wheel carrying a prebuilt
executable plus a launcher that hands the process over to it.

Nothing here reimplements any CLI behaviour — see docs/design/new_cli.md §4.6
and G5. The binary is the same artifact the store serves and the install script
fetches, so there is exactly one implementation of `sbs`.
"""

__all__ = ["main", "binary_path"]

from .launcher import binary_path, main
