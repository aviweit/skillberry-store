# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""The `skillberry-store-cli` distribution and the SDK's `[cli]` extra.

docs/design/new_cli.md §4.6 and G5: a Go binary cannot be a `pip` console
script, so the CLI is distributed as platform wheels carrying the binary plus a
launcher. This is the structural guard on that arrangement — the bits that, if
they drift, produce a package that installs cleanly and then does not work.

The functional check (build a wheel, install it, run `sbs`) lives in CI, where
the artifacts exist; here everything is read off disk so the tests run in any
checkout.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CLI_PKG = REPO_ROOT / "packaging" / "skillberry-store-cli"
SDK_DIR = REPO_ROOT / "client" / "python" / "skillberry_store_sdk"

# The five platforms of §5.1. A wheel tag missing here means `pip` silently has
# nothing to install for that platform.
EXPECTED_PLATFORMS = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)


@pytest.fixture(scope="module")
def cli_pyproject() -> dict:
    path = CLI_PKG / "pyproject.toml"
    assert path.is_file(), f"{path} is missing"
    with path.open("rb") as fh:
        return tomllib.load(fh)


def test_package_declares_the_sbs_console_script(cli_pyproject):
    """This is the whole reason the package exists."""
    scripts = cli_pyproject["project"]["scripts"]
    assert scripts.get("sbs") == "skillberry_store_cli.launcher:main", (
        f"the sbs entry point is {scripts.get('sbs')!r}; `pip install "
        f"skillberry-store-cli` must put `sbs` on the PATH"
    )


def test_package_has_no_runtime_dependencies(cli_pyproject):
    """A wheel that delivers one static binary must not constrain anyone's env.

    Any dependency here is a potential resolver conflict in someone else's
    project, for no benefit: the actual program is a static Go binary.
    """
    assert cli_pyproject["project"]["dependencies"] == [], (
        "skillberry-store-cli must have no dependencies"
    )


def test_package_ships_the_binary_as_package_data(cli_pyproject):
    """Without this the wheel contains a launcher and nothing to launch."""
    data = cli_pyproject["tool"]["setuptools"]["package-data"]["skillberry_store_cli"]
    assert "bin/sbs" in data
    assert "bin/sbs.exe" in data, "the Windows wheel needs the .exe pattern"
    # We redistribute an MIT-licensed binary, so its licence ships with it.
    assert "bin/LICENSE.restish" in data


def test_launcher_execs_rather_than_spawning_on_posix():
    """`os.execv`, not `subprocess`, is a deliberate choice.

    Replacing the process is what makes signals reach the program the user is
    running, keeps the exit status exact with no translation layer, avoids a
    second process and an orphan risk, and hands the TTY to the binary so colour
    and interactive prompts behave as they do for a directly-installed binary.
    Spawning would break each of those in a way that only shows up under Ctrl-C
    or in a CI exit code.
    """
    source = (CLI_PKG / "src" / "skillberry_store_cli" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert "os.execv" in source, (
        "the launcher must execv the binary on POSIX; spawning breaks signal "
        "delivery and exit-status fidelity"
    )
    # Windows has no usable execv, so a spawn there is correct — but it must be
    # guarded rather than being the general path.
    assert re.search(r"os\.name\s*==\s*['\"]nt['\"]", source), (
        "the subprocess fallback must be guarded to Windows only"
    )


def test_launcher_reports_a_missing_binary_actionably():
    """An sdist install, or a wheel for the wrong platform, must explain itself.

    Otherwise the failure is a bare FileNotFoundError from execv, which tells the
    user nothing about which of the several remedies applies.
    """
    source = (CLI_PKG / "src" / "skillberry_store_cli" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert "--only-binary" in source, (
        "name the pip flag that forces a platform wheel"
    )
    assert "/cli/download" in source, (
        "offer a store download, which works where no wheel exists"
    )


def test_launcher_parses_and_exposes_main():
    launcher = CLI_PKG / "src" / "skillberry_store_cli" / "launcher.py"
    tree = ast.parse(launcher.read_text(encoding="utf-8"))
    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert {"main", "binary_path"} <= functions


def test_build_script_covers_every_supported_platform():
    """A platform absent from PLATFORM_TAGS gets no wheel at all."""
    source = (CLI_PKG / "build_wheels.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tags: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PLATFORM_TAGS" for t in node.targets
        ):
            tags = ast.literal_eval(node.value)
    assert tags, "PLATFORM_TAGS not found in build_wheels.py"

    missing = [p for p in EXPECTED_PLATFORMS if p not in tags]
    assert not missing, f"no wheel platform tag for {missing}"


def test_wheel_tags_are_pypi_acceptable():
    """`linux_x86_64` is rejected by PyPI; manylinux is required.

    The binaries are built with CGO_ENABLED=0 and so have no glibc floor at all —
    the manylinux tag is a formality that makes them installable, not a claim
    about linking. Getting it wrong means the upload fails at release time.
    """
    source = (CLI_PKG / "build_wheels.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tags: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PLATFORM_TAGS" for t in node.targets
        ):
            tags = ast.literal_eval(node.value)

    for platform, tag in tags.items():
        if platform.startswith("linux-"):
            assert "manylinux" in tag, f"{platform} uses {tag!r}, which PyPI rejects"
        elif platform.startswith("darwin-"):
            assert tag.startswith("macosx_"), f"{platform} uses {tag!r}"
        elif platform.startswith("windows-"):
            assert tag.startswith("win"), f"{platform} uses {tag!r}"


def test_build_script_retags_wheels_per_platform():
    """`pip wheel` produces py3-none-any, which would claim to work everywhere.

    Without the retag every wheel would be installable on every platform and the
    last one uploaded would win — users would get whichever binary that was.
    """
    source = (CLI_PKG / "build_wheels.py").read_text(encoding="utf-8")
    assert "--platform-tag" in source, (
        "build_wheels.py must retag each wheel with its platform tag"
    )
    assert "--remove" in source, (
        "the original py3-none-any wheel must be removed, or it would be uploaded "
        "alongside the platform wheels and shadow them"
    )


def test_build_script_copies_the_engine_licence():
    source = (CLI_PKG / "build_wheels.py").read_text(encoding="utf-8")
    assert "LICENSE.restish" in source, (
        "the wheel must carry restish's MIT licence; we redistribute its code"
    )


# --------------------------------------------------------------------------- #
# The SDK's [cli] extra
# --------------------------------------------------------------------------- #


def test_sdk_declares_the_cli_extra():
    """`pip install skillberry-store-sdk[cli]` is the documented migration."""
    pyproject = SDK_DIR / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("generated SDK not present in this checkout")
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)

    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "cli" in extras, (
        "the SDK declares no [cli] extra, so the CHANGELOG's documented remedy "
        "`pip install skillberry-store-sdk[cli]` would fail"
    )
    joined = " ".join(extras["cli"])
    assert "skillberry-store-cli" in joined


def test_cli_extra_is_gated_by_environment_markers():
    """The SDK must stay installable where no wheel exists.

    An unconditional dependency would make `pip install skillberry-store-sdk[cli]`
    fail outright on, say, linux-riscv64 — where the right outcome is "you get the
    SDK, and the CLI comes from the store's install script".
    """
    pyproject = SDK_DIR / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("generated SDK not present in this checkout")
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)

    requirement = " ".join(data["project"]["optional-dependencies"]["cli"])
    assert ";" in requirement, (
        f"the cli extra has no environment marker: {requirement!r}"
    )
    for marker in ("sys_platform", "platform_machine"):
        assert marker in requirement, f"{marker} missing from the marker"


def test_sdk_setup_py_declares_the_extra_too():
    """Both build paths must agree, since either can produce the distribution."""
    setup_py = SDK_DIR / "setup.py"
    if not setup_py.is_file():
        pytest.skip("generated SDK not present in this checkout")
    source = setup_py.read_text(encoding="utf-8")
    assert "extras_require" in source
    assert "skillberry-store-cli" in source


def test_sdk_generation_reinstates_the_extra():
    """`make generate-sdk` wipes the SDK directory, so it must re-add the extra.

    Without this the extra would silently vanish the next time anyone regenerated
    the SDK, and the CHANGELOG's remedy would stop working with no failing test.
    """
    shared = REPO_ROOT / "skillberry-common" / ".mk" / "dev.mk"
    if not shared.is_file():
        pytest.skip("skillberry-common subtree not present in this checkout")
    text = shared.read_text(encoding="utf-8")
    assert "SDK_CLI_EXTRA_PY" in text, (
        "generate-sdk does not re-add the [cli] extra, so regenerating the SDK "
        "would drop it"
    )
    assert "optional-dependencies" in text
