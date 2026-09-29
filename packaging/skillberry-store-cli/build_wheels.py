#!/usr/bin/env python3
# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Build one `skillberry-store-cli` wheel per platform.

docs/design/new_cli.md §4.6 / G5. Reads the artifacts produced by
``client/go/build.sh`` and emits a platform-tagged wheel for each, so that
``pip install skillberry-store-cli`` resolves to the right binary automatically.

Usage::

    python packaging/skillberry-store-cli/build_wheels.py \
        --artifacts cli-prebuilt --out dist

Why this is a script rather than a `cibuildwheel` config: there is nothing to
*compile* per platform. The binaries already exist (cross-compiled from one
Linux host), so all that is needed is to drop each one into the package and tag
the wheel. Driving cibuildwheel would mean five native runners to do no
compilation, which is the cost the Go pivot removed in the first place.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Platform id -> the wheel platform tag `pip` matches against.
#
# manylinux_2_17 rather than a plain `linux_x86_64`: PyPI rejects the latter, and
# the binaries are built with CGO_ENABLED=0 so they have no glibc floor at all —
# the tag is a formality that makes them installable, not a claim about linking.
PLATFORM_TAGS = {
    "linux-amd64": "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "linux-arm64": "manylinux_2_17_aarch64.manylinux2014_aarch64",
    # macOS 11 is the floor Apple Silicon requires; the amd64 wheel claims the
    # same to keep one story.
    "darwin-amd64": "macosx_11_0_x86_64",
    "darwin-arm64": "macosx_11_0_arm64",
    "windows-amd64": "win_amd64",
}


def _read_manifest(artifacts: Path) -> dict:
    path = artifacts / "prebuilt-manifest.json"
    if not path.is_file():
        sys.exit(
            f"{path} not found. Build the artifacts first:\n"
            f"  make cli-dist    (or ./client/go/build.sh --out {artifacts})"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _pep440_version(raw: str) -> str:
    """Turn a git-describe-ish version into something `pip` will accept.

    ``0.0.0+g3e46b0d`` is already valid PEP 440 (a local version segment), but a
    bare commit hash or a tag with a leading ``v`` is not. Normalising here rather
    than at the call site keeps every wheel's metadata installable — an invalid
    version is rejected by `pip` at install time, long after the build looked
    like it worked.
    """
    version = raw.strip().lstrip("v")
    if re.fullmatch(r"\d+(\.\d+)*([-.]?(a|b|rc)\d+)?(\+[A-Za-z0-9.]+)?", version):
        return version
    # Anything else becomes a local-version label on 0.0.0, which is valid and
    # sorts below any real release.
    sanitised = re.sub(r"[^A-Za-z0-9.]+", ".", version).strip(".")
    return f"0.0.0+{sanitised or 'unknown'}"


def build_one(
    package_root: Path,
    artifacts: Path,
    out_dir: Path,
    platform: str,
    filename: str,
    version: str,
) -> Path:
    """Build the wheel for one platform, in an isolated copy of the package."""
    tag = PLATFORM_TAGS.get(platform)
    if tag is None:
        sys.exit(f"no wheel platform tag known for {platform!r}")

    source_binary = artifacts / platform / filename
    if not source_binary.is_file():
        sys.exit(f"artifact not found: {source_binary}")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "pkg"
        # A copy per platform, because the binary lands at a fixed path inside
        # the package: building in place would leave the previous platform's
        # binary behind and silently ship, say, the Linux binary in the Windows
        # wheel.
        shutil.copytree(package_root, staging, ignore=shutil.ignore_patterns("dist", "build", "*.egg-info"))

        bin_dir = staging / "src" / "skillberry_store_cli" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_binary, bin_dir / filename)
        # 0755 inside the wheel; the launcher re-chmods on install as a backstop,
        # because not every install path preserves the bit.
        (bin_dir / filename).chmod(0o755)

        licence = artifacts / "LICENSE.restish"
        if licence.is_file():
            # We redistribute a restish-derived binary, so its MIT licence ships
            # in the wheel too — not only in the store's archives.
            shutil.copy2(licence, bin_dir / "LICENSE.restish")

        pyproject = staging / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        text = re.sub(r'^version = "[^"]*"', f'version = "{version}"', text, count=1, flags=re.MULTILINE)
        pyproject.write_text(text, encoding="utf-8")

        wheel_dir = Path(tmp) / "wheel"
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(staging)],
            check=True,
        )

        built = next(wheel_dir.glob("*.whl"))
        # `pip wheel` tags a pure-Python package `py3-none-any`; retag it so pip
        # only ever installs the wheel matching the user's platform. Without this
        # every wheel would claim to work everywhere and the last one uploaded
        # would win.
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                sys.executable, "-m", "wheel", "tags",
                "--python-tag", "py3",
                "--abi-tag", "none",
                "--platform-tag", tag,
                "--remove",
                str(built),
            ],
            check=True,
            cwd=str(wheel_dir),
        )
        retagged = next(wheel_dir.glob("*.whl"))
        destination = out_dir / retagged.name
        shutil.copy2(retagged, destination)
        return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        default="cli-prebuilt",
        type=Path,
        help="directory produced by client/go/build.sh (default: cli-prebuilt)",
    )
    parser.add_argument(
        "--out", default="dist", type=Path, help="where to write the wheels"
    )
    parser.add_argument(
        "--platforms",
        nargs="*",
        help="limit to these platform ids (default: everything in the manifest)",
    )
    args = parser.parse_args()

    package_root = Path(__file__).resolve().parent
    manifest = _read_manifest(args.artifacts)
    version = _pep440_version(manifest.get("cli_version") or "0.0.0")

    wanted = set(args.platforms or [])
    built: list[Path] = []
    for entry in manifest.get("artifacts", []):
        platform = entry["platform"]
        if wanted and platform not in wanted:
            continue
        wheel = build_one(
            package_root,
            args.artifacts,
            args.out,
            platform,
            entry["filename"],
            version,
        )
        print(f"built {wheel.name}")
        built.append(wheel)

    if not built:
        sys.exit("no wheels were built; check --artifacts and --platforms")

    print(f"\n{len(built)} wheel(s) in {args.out} at version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
