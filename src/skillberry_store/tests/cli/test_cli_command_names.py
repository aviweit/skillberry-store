"""The reserved-name guard for the native CLI's promoted command surface.

docs/design/new_cli.md §8.1 #5. The Go CLI embeds restish with
``SetCommandSurface{PromotedAPI: "store", SupportCommandNamespace: "cli"}``, so
every generated operation lands at the *root* of the command tree — ``sbs
list-skills``, not ``sbs store list-skills`` (§4.2).

That is the whole ergonomic point, and it creates one failure mode worth a test:
if an endpoint's ``x-cli-name`` ever equals the support namespace, restish
refuses to start with

    command surface: promoted operation "cli" collides with support command
    namespace "cli"; choose another SupportCommandNamespace or hide support
    commands

which is a *runtime* failure of the shipped binary, triggered by an endpoint
added on the Python side. This test moves that discovery to PR time, where the
person who added the endpoint can see it, rather than to release time — or to a
user's terminal.

The same applies to the verbs the CLI handles itself before restish parses argv
(``connect``, ``download-cli``, ``self-update``): an operation with one of those
names would be silently shadowed, which is worse than a collision error because
nothing would report it at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CLI_GO_DIR = REPO_ROOT / "client" / "go" / "cli"
CLI_TESTS_DIR = REPO_ROOT / "client" / "go" / "tests"


def _cli_names_from_source() -> set[str]:
    """Collect every ``x-cli-name`` declared under fast_api/.

    Read from the source rather than from a constructed ``SBS`` app on purpose:
    building the app pulls in the vector DB, the plugin loader and the object
    handlers, which makes a guard against a one-line typo cost seconds and
    depend on unrelated subsystems being importable. The marker is a literal in
    an ``openapi_extra`` dict, so grepping for it is both sufficient and exact.
    """
    names: set[str] = set()
    pattern = re.compile(r'"x-cli-name":\s*"([^"]+)"')
    for path in (REPO_ROOT / "src" / "skillberry_store" / "fast_api").rglob("*.py"):
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def _go_const(name: str) -> str:
    """Read a single-quoted Go string constant out of the CLI sources.

    Keeps this test honest about what the binary actually uses: hardcoding
    ``"cli"`` here would keep passing if someone changed the namespace in Go,
    which is precisely the change that could introduce a collision.
    """
    pattern = re.compile(rf'^const {re.escape(name)}\s*=\s*"([^"]*)"', re.MULTILINE)
    for path in CLI_GO_DIR.glob("*.go"):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    raise AssertionError(f"could not find `const {name}` in {CLI_GO_DIR}")


def _local_verbs() -> set[str]:
    """The verbs LocalVerb() claims before restish sees argv (client/go/cli/verbs.go)."""
    source = (CLI_GO_DIR / "verbs.go").read_text(encoding="utf-8")
    body = re.search(r"switch argv\[1\] \{(.*?)\n\t\}", source, re.DOTALL)
    assert body, "could not locate the LocalVerb switch in client/go/cli/verbs.go"
    return set(re.findall(r'case "([^"]+)":', body.group(1)))


# restish's own support commands, which a promoted surface keeps reachable. With
# a SupportCommandNamespace set they live under it, so only the namespace itself
# can collide — but if that namespace is ever removed, these become root
# commands and every one of them becomes a collision risk. Listed so the test
# stays correct under that change.
RESTISH_SUPPORT_COMMANDS = frozenset(
    {"auth", "cache", "completion", "config", "doctor", "version"}
)

# Operations the CLI deliberately shadows with a local verb, and why. Every entry
# is a case where the generated operation technically exists but is the *wrong*
# thing to run, so shadowing is the feature rather than the bug.
#
# Each of these is a decision, not an oversight — which is why they are listed
# here with reasons rather than simply excluded from the check.
INTENTIONAL_SHADOWS = frozenset(
    {
        # POST /auth/login. The generated operation would take the credentials as
        # a JSON request body, which means a password on the command line and in
        # the shell history. The local verb prompts instead — the same reason the
        # Python shim intercepted it (§4.3).
        "login",
        # POST /auth/logout. The generated operation revokes server-side but
        # leaves the token cached locally, so the next command silently signs the
        # user back in. The local verb does both, in that order.
        "logout",
        # GET /cli/download. The generated operation would stream ~32 MB of
        # octet-stream to stdout. The local verb resolves the running platform
        # exactly (runtime.GOOS/GOARCH), verifies the sha256 against the manifest
        # before writing, sets the executable bit, and renames atomically — and it
        # works when the store's spec is unreachable, which is when you need it
        # (§5.8). The endpoint keeps the name because it describes the endpoint,
        # and other clients (the generated SDK, plain restish) still use it.
        "download-cli",
    }
)


def test_no_cli_name_collides_with_support_namespace():
    """An operation named like the support namespace makes the binary refuse to start."""
    namespace = _go_const("SupportNamespace")
    collisions = sorted(n for n in _cli_names_from_source() if n == namespace)
    assert not collisions, (
        f"x-cli-name {collisions} collides with the CLI's support command "
        f"namespace {namespace!r}. The shipped binary would refuse to start. "
        f"Rename the endpoint's x-cli-name, or change SupportNamespace in "
        f"client/go/cli/branding.go."
    )


def test_no_cli_name_is_silently_shadowed_by_a_local_verb():
    """A local verb shadows a generated operation with no error at all."""
    verbs = _local_verbs()
    assert verbs, "expected LocalVerb to claim at least one verb"

    cli_names = _cli_names_from_source()
    shadowed = sorted((cli_names & verbs) - INTENTIONAL_SHADOWS)
    assert not shadowed, (
        f"x-cli-name {shadowed} is shadowed by a local verb in client/go/cli/verbs.go, "
        f"so typing it would never reach the store and nothing would report it. "
        f"Rename the endpoint, remove the verb, or — if the shadow is deliberate "
        f"— add it to INTENTIONAL_SHADOWS with the reason."
    )


def test_every_intentional_shadow_still_shadows_something():
    """Keep the waiver list honest in both directions.

    The list above waives a real check, so a stale entry is a hole: it would go
    on silencing the guard for a name that no longer needs it, and the next
    genuine collision on that name would pass unnoticed.
    """
    cli_names = _cli_names_from_source()
    stale = sorted(INTENTIONAL_SHADOWS - cli_names)
    assert not stale, (
        f"{stale} are listed as intentional shadows but are no longer generated "
        f"operations. Drop them from INTENTIONAL_SHADOWS — and check whether the "
        f"corresponding local verb in client/go/cli/ is still wanted."
    )


def test_every_intentional_shadow_is_actually_implemented_locally():
    """A waived name with no local verb means the operation is simply gone.

    If `download-cli` were waived here but never implemented in Go, `sbs
    download-cli` would be an unknown command — the waiver would be hiding a
    missing feature rather than documenting a deliberate override.
    """
    verbs = _local_verbs()
    go_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in CLI_GO_DIR.glob("*.go")
    )
    for name in sorted(INTENTIONAL_SHADOWS):
        # Either claimed by localVerb (connect/download-cli/self-update) or
        # dispatched through the engine in main.go's switch (login/logout).
        assert name in verbs or f'case "{name}":' in go_sources, (
            f"{name!r} is waived as an intentional shadow but no local verb "
            f"handles it, so typing it would reach neither the store nor a local "
            f"implementation."
        )


def test_support_commands_do_not_collide_when_namespaced():
    """With a namespace configured, restish's support commands cannot collide.

    This documents *why* the collision surface is only one name: a namespace
    moves `auth`/`cache`/`config`/`doctor`/`completion`/`version` under it. Drop
    the namespace and all six return to the root, so this assertion is what
    tells you the guard above would need widening.
    """
    namespace = _go_const("SupportNamespace")
    assert namespace, (
        "SupportNamespace is empty, so restish's support commands sit at the "
        "root and any of "
        f"{sorted(RESTISH_SUPPORT_COMMANDS)} could collide with an operation. "
        "Widen test_no_cli_name_collides_with_support_namespace to check all of "
        "them."
    )
    overlap = sorted(_cli_names_from_source() & RESTISH_SUPPORT_COMMANDS)
    if overlap:
        pytest.fail(
            f"x-cli-name {overlap} shares a name with a restish support command. "
            f"It is safe today only because supportNamespace={namespace!r} moves "
            f"them off the root; it would break if that were removed."
        )


def test_promoted_api_name_is_not_itself_an_operation():
    """``store`` is the configured API key, not a command, but keep them distinct.

    A generated operation named ``store`` would read as if it addressed the API
    itself, which is the two-level shape (``sbs store list-skills``) this design
    removed.
    """
    api_name = _go_const("APIName")
    assert api_name not in _cli_names_from_source(), (
        f"an operation is named {api_name!r}, the promoted API's own key; "
        f"rename it to keep the root surface unambiguous."
    )


# --------------------------------------------------------------------------- #
# The §3.3 "restish" allowlist exists in three places and must agree
# --------------------------------------------------------------------------- #
#
# The inventory of surviving "restish" strings is asserted in three independent
# gates, each of which can only see part of the picture:
#
#   * client/go/tests/main_test.go   — the count, as a Go unit test
#   * tests/cli/test_native_cli_e2e.py — a built binary on this platform
#   * .github/workflows/cli-artifacts.yml — a built binary on all five OSes
#
# Three copies is a deliberate trade (each gate must run standalone), but a copy
# that drifts silently widens the allowlist on one path while the others keep
# failing — or worse, keeps passing after upstream fixes a string, hiding the
# fix. This test makes the divergence itself a failure.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cli-artifacts.yml"
E2E_TEST = REPO_ROOT / "src" / "skillberry_store" / "tests" / "cli" / "test_native_cli_e2e.py"

# The four §3.3 entries plus the one we emit deliberately in `--version`.
EXPECTED_ALLOWLIST = (
    "--help-all",
    "--rsh-config",
    "Restish version:",
    "restish shell setup",
    "engine: restish",
    "Embeds restish",
)


def test_go_inventory_lists_the_four_residual_strings():
    source = (CLI_TESTS_DIR / "main_test.go").read_text(encoding="utf-8")
    block = re.search(
        r"var knownRestishStrings = \[\]string\{(.*?)\n\}", source, re.DOTALL
    )
    assert block, "knownRestishStrings not found in client/go/tests/main_test.go"
    entries = re.findall(r'"([^"]+)"', block.group(1))
    # The Go list covers only §3.3 — the `engine: restish` string is ours, not
    # upstream's, so it is deliberately not part of that inventory.
    assert len(entries) == 4, (
        f"knownRestishStrings has {len(entries)} entries, want exactly 4 "
        f"(docs/design/new_cli.md §3.3): {entries}"
    )


@pytest.mark.parametrize("needle", EXPECTED_ALLOWLIST)
def test_e2e_allowlist_covers_every_expected_string(needle):
    source = E2E_TEST.read_text(encoding="utf-8")
    block = re.search(
        r"ALLOWED_RESTISH_SUBSTRINGS = \((.*?)\n\)", source, re.DOTALL
    )
    assert block, "ALLOWED_RESTISH_SUBSTRINGS not found in test_native_cli_e2e.py"
    assert needle in block.group(1), (
        f"{needle!r} is missing from the e2e allowlist, so that gate is stricter "
        f"than the others and will fail on a string the design accepts"
    )


@pytest.mark.parametrize("needle", EXPECTED_ALLOWLIST)
def test_ci_workflow_allowlist_covers_every_expected_string(needle):
    """The CI gate runs on five OSes, so a missing entry breaks the build there.

    And an *extra* entry is worse: it would silence a real regression on every
    platform at once, which is the only place the branding is checked against a
    real macOS or Windows binary.
    """
    if not WORKFLOW.is_file():
        pytest.skip("cli-artifacts workflow not present in this checkout")
    source = WORKFLOW.read_text(encoding="utf-8")
    assert needle in source, (
        f"{needle!r} is not in .github/workflows/cli-artifacts.yml's grep -v "
        f"chain, so the CI branding gate is stricter than the local one"
    )


def test_ci_workflow_allowlist_is_not_wider_than_expected():
    """No extra `grep -v` beyond the agreed allowlist.

    An extra exclusion silently accepts a new "restish" mention on every
    platform, which is exactly what the gate exists to catch.
    """
    if not WORKFLOW.is_file():
        pytest.skip("cli-artifacts workflow not present in this checkout")
    source = WORKFLOW.read_text(encoding="utf-8")
    # The branding step's exclusions only; other `grep -v` uses in the file would
    # be unrelated, so scope to the step.
    step = re.search(
        r"Assert no unexpected \"restish\".*?(?=\n      - name:)", source, re.DOTALL
    )
    assert step, "could not locate the branding assertion step"
    exclusions = re.findall(r"grep -v(?: --)? '([^']+)'", step.group(0))
    unexpected = [e for e in exclusions if e not in EXPECTED_ALLOWLIST]
    assert not unexpected, (
        f"the CI branding gate excludes {unexpected}, which is not in the agreed "
        f"§3.3 allowlist. Either upstream changed (update EXPECTED_ALLOWLIST here, "
        f"client/go/tests/main_test.go and test_native_cli_e2e.py together) or this "
        f"is a "
        f"regression being silenced."
    )
