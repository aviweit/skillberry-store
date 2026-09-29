"""The generated SDK is a pure Python library and ships no ``sbs`` command.

docs/design/new_cli.md §8.3 #21c — the regression test for the deletion in §4.6.

There is now exactly one implementation of ``sbs``: the native Go binary in
``client/go/cli`` that embeds restish as a library. The Python shim that used to be
injected into the generated SDK is gone, and it is not coming back as a
fallback — its subprocess model is precisely what made the CLI's output
impossible to brand (§10).

This is also the guard on the one **breaking change** the design accepts (G7,
§9.1): ``pip install skillberry-store-sdk`` no longer provides ``sbs``. The
remedy is in the CHANGELOG under the identifier ``sbs console script``, and
``test_changelog.py`` keeps that note findable.

The SDK itself must stay pure Python and universal — it is a library and has to
install anywhere, including platforms with no CLI wheel — so what is asserted
here is the *absence* of a console script, not the presence of a binary.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DIR = REPO_ROOT / "client" / "python" / "skillberry_store_sdk"
SDK_PKG = SDK_DIR / "skillberry_store_sdk"


def test_sdk_cli_module_is_absent():
    """``sdk_cli.py`` must not be in the generated SDK."""
    shim = SDK_PKG / "sdk_cli.py"
    assert not shim.exists(), (
        f"{shim.relative_to(REPO_ROOT)} is back. The Python shim was deleted in "
        f"favour of the native Go CLI (docs/design/new_cli.md §4.6); two "
        f"implementations of one CLI would drift, and the shim's subprocess "
        f"model is the problem the Go CLI exists to solve. If `make generate-sdk` "
        f"recreated it, check that SDK_PY_CLI is still 0 in .mk/local.mk."
    )


def test_setup_py_declares_no_sbs_console_script():
    setup_py = SDK_DIR / "setup.py"
    if not setup_py.exists():
        pytest.skip("generated SDK not present in this checkout")
    text = setup_py.read_text(encoding="utf-8")

    assert "sdk_cli" not in text, (
        "setup.py still references sdk_cli; the generated SDK must declare no "
        "CLI entry point."
    )
    # Matched on the marker rather than on the exact literal so a reformatted or
    # renamed entry point is still caught.
    assert "console_scripts" not in text, (
        "setup.py declares console_scripts. The SDK is a library; `sbs` is the "
        "native binary from client/go/cli, distributed as platform wheels."
    )


def test_pyproject_declares_no_sbs_script():
    pyproject = SDK_DIR / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("generated SDK not present in this checkout")

    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)

    poetry_scripts = data.get("tool", {}).get("poetry", {}).get("scripts", {})
    assert "sbs" not in poetry_scripts, (
        f"[tool.poetry.scripts] declares sbs = {poetry_scripts.get('sbs')!r}. "
        f"The SDK must ship no console script."
    )
    project_scripts = data.get("project", {}).get("scripts", {})
    assert "sbs" not in project_scripts, (
        "[project.scripts] declares sbs; the SDK must ship no console script."
    )


def test_sdk_generation_is_gated_off_for_this_asset():
    """``SDK_PY_CLI := 0`` in .mk/local.mk is what keeps the shim from returning.

    Without it the next `make generate-sdk` would helpfully re-inject the shim
    and re-add the entry point, quietly undoing the deletion — so the flag is
    part of the change, not a convenience.
    """
    local_mk = (REPO_ROOT / ".mk" / "local.mk").read_text(encoding="utf-8")
    match = re.search(r"^SDK_PY_CLI\s*:?=\s*(\S+)", local_mk, re.MULTILINE)
    assert match, (
        "SDK_PY_CLI is not set in .mk/local.mk. It defaults to 1 in "
        "skillberry-common/.mk/dev.mk, so `make generate-sdk` would re-inject "
        "the deleted Python shim."
    )
    assert match.group(1) == "0", (
        f"SDK_PY_CLI = {match.group(1)!r}, want 0 for this asset."
    )


def test_shared_template_still_defaults_to_on_for_sibling_assets():
    """The gate must not break sibling assets that still use the shim (G6).

    The template lives in a shared subtree, so deleting it outright — or
    flipping its default — would take the CLI away from every other asset. The
    flag defaults to 1 there and is set to 0 here; the template is removed from
    skillberry-common only once no asset opts in.
    """
    shared = REPO_ROOT / "skillberry-common" / ".mk" / "dev.mk"
    if not shared.exists():
        pytest.skip("skillberry-common subtree not present in this checkout")
    text = shared.read_text(encoding="utf-8")
    match = re.search(r"^SDK_PY_CLI\s*\?=\s*(\S+)", text, re.MULTILINE)
    assert match, (
        "skillberry-common/.mk/dev.mk does not define a default for SDK_PY_CLI; "
        "sibling assets would lose their generated CLI."
    )
    assert match.group(1) == "1", (
        f"the shared default is {match.group(1)!r}, want 1 so sibling assets "
        f"that still rely on the shim are unaffected."
    )


def test_shared_template_carries_a_deprecation_note():
    """Whoever reads the template next should learn it is a dead end."""
    template = REPO_ROOT / "skillberry-common" / "scripts" / "sdk_cli.py"
    if not template.exists():
        pytest.skip("skillberry-common subtree not present in this checkout")
    head = template.read_text(encoding="utf-8")[:3000]
    assert "DEPRECATED" in head, (
        "the shared sdk_cli.py template carries no deprecation note; a sibling "
        "asset could adopt it without learning why it was abandoned."
    )
    assert "new_cli.md" in head, (
        "the deprecation note should point at the design that replaced it."
    )
