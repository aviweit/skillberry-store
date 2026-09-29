"""End-to-end assertions on a *built* native CLI binary.

docs/design/new_cli.md §8.1 #6, §8.2 #12 and §8.3 #19. These are the tests that
cannot be written in Go: they need a real linked binary and a running store, and
what they assert is the thing users actually see — the help text restish renders
and the exit status the process returns.

Every test skips without a Go toolchain. A `pip`-only contributor must be able to
run `make test`; building the CLI is a release-time concern (§G10).

Three properties are pinned here:

1. **The branded surface.** Root usage says ``sbs``, operations are at the root,
   and the word "restish" appears nowhere outside the four known strings of
   §3.3. This is the regression gate for Feature A, on the real binary.
2. **Zero configuration.** ``sbs list-skills`` against the store returns data
   from a clean ``HOME`` with no config file and no ``connect`` — measurement M2
   turned into a test, and the whole promise of a downloaded artifact.
3. **The ``-ldflags`` control.** A binary linked against a dead port must
   *fail*. ``-X`` silently no-ops unless the target has a constant string
   initializer (§3.4 #1, G8), and the linker reports nothing. The happy path
   looks identical either way, so only a build that *should* fail can tell them
   apart. Without this test, "the URL was baked in" is unverified.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
GO_ROOT = REPO_ROOT / "client" / "go"
CLI_PKG = "github.com/skillberry-ai/skillberry-store/client/go/cli"
CMD_PKG = "./cli/cmd/sbs"

# Where the e2e fixture serves the store (see conftest.wait_until_server_ready).
STORE_URL = "http://127.0.0.1:8000"

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None,
    reason="no Go toolchain; the native CLI is a release-time artifact (§G10)",
)


def _build(tmp_path: Path, url: str, name: str = "sbs") -> Path:
    """Local build with ``url`` baked into the slot.

    The ``-X`` targets name the ``cli`` package's full import path, not ``main``:
    the injected variables live in the importable library so that the Go test
    suite can live in ``client/go/tests``. A stale ``-X main.urlSlot=`` is
    accepted silently by the linker and injects nothing, which is exactly the
    failure ``test_dead_port_build_must_fail`` below exists to catch.
    """
    out = tmp_path / name
    ldflags = " ".join(
        [
            "-s",
            "-w",
            f"-X {CLI_PKG}.URLSlot={url}",
            f"-X {CLI_PKG}.Version=e2e-test",
            f"-X {CLI_PKG}.EngineVersion=test",
        ]
    )
    proc = subprocess.run(
        ["go", "build", "-trimpath", "-ldflags", ldflags, "-o", str(out), CMD_PKG],
        cwd=GO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "CGO_ENABLED": "0"},
        timeout=600,
    )
    assert proc.returncode == 0, f"go build failed:\n{proc.stderr}"
    return out


def _run(binary: Path, *args: str, home: Path, timeout: int = 120):
    """Run the binary with an isolated HOME and no inherited stdin.

    ``stdin=DEVNULL`` matters: an interactive prompt would otherwise block on the
    test runner's stdin and hang the suite instead of failing.
    """
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        # Deliberately not inheriting SBS_URL / SBS_TOKEN: these tests are about
        # what the *baked* artifact does with no help from the environment.
    }
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


# The §3.3 inventory. Asserted to be exactly four entries by the Go test
# TestKnownRestishStringInventoryIsExact, and kept in sync with it by hand —
# deliberately, so that an upstream fix requires a visible edit in both places.
#
# Plus one string we emit on purpose: `sbs --version` reports the embedded engine
# version, because a bug report needs both numbers and hiding which restish is
# linked in would be worse than saying it.
ALLOWED_RESTISH_SUBSTRINGS = (
    "--help-all",  # "Show all inherited Restish flags in help" (no public hook)
    "--rsh-config",  # "Path to the restish config file ..." (no public hook)
    "Restish version:",  # `cli doctor`'s label
    "restish shell setup",  # `cli doctor`'s hint
    "engine: restish",  # ours, on purpose
    "Embeds restish",  # ours: the licence pointer required by redistribution
)


def _unexpected_restish_lines(text: str, *, scrub: str | None = None) -> list[str]:
    """Lines mentioning restish that are not on the allowlist.

    ``scrub`` removes a path prefix before matching. This is not cosmetic: the
    tests run under a pytest tmp directory whose name is derived from the test's
    own name, so a test *about* restish mentions gets a HOME like
    ``.../test_no_unexpected_restish_men0/`` — and every path ``cli doctor``
    prints then contains the word. What is under test is the CLI's own
    vocabulary, not the directory the harness happened to choose.
    """
    bad = []
    for line in text.splitlines():
        candidate = line.replace(scrub, "<home>") if scrub else line
        if "restish" not in candidate.lower():
            continue
        if any(allowed in candidate for allowed in ALLOWED_RESTISH_SUBSTRINGS):
            continue
        bad.append(candidate.strip())
    return bad


@pytest.fixture(scope="module")
def built_cli(tmp_path_factory):
    """One binary per module, baked to talk to the e2e store."""
    return _build(tmp_path_factory.mktemp("cli-build"), STORE_URL)


@pytest.fixture
def clean_home(tmp_path):
    """A HOME with no config, no cache and no token — a fresh install."""
    home = tmp_path / "home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# 1. The branded surface
# ---------------------------------------------------------------------------


def test_version_is_branded(built_cli, clean_home):
    proc = _run(built_cli, "--version", home=clean_home)
    assert proc.returncode == 0, proc.stderr
    # restish renders "<command> version <value>". If versionLine() repeated the
    # name this would read "sbs version sbs e2e-test".
    assert proc.stdout.startswith("sbs version e2e-test"), proc.stdout
    assert re.search(r"engine: restish", proc.stdout), (
        "the engine version belongs in --version output for bug reports"
    )


@pytest.mark.usefixtures("run_sbs")
def test_root_help_is_branded_and_promotes_operations(built_cli, clean_home):
    """M1: operations at the root, support commands namespaced, no `sbs sbs`."""
    proc = _run(built_cli, "--help", home=clean_home)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    out = proc.stdout

    assert "sbs [flags]" in out, f"root usage is not branded:\n{out}"

    # Generated operations must be root commands. This is the ergonomic point of
    # PromotedAPI, and the thing the shim could not do at all.
    for op in ("list-skills", "get-skill", "list-tools"):
        assert re.search(rf"^\s+{re.escape(op)}\b", out, re.MULTILINE), (
            f"{op!r} is not a root command:\n{out}"
        )

    # The shim's shape. `sbs sbs list-skills` is what a renamed binary would
    # have printed, and it is the reason this design exists.
    assert "sbs sbs " not in out, f"doubled command name in help:\n{out}"

    # Support commands live under the branded namespace, not at the root.
    assert re.search(r"^\s+cli\b", out, re.MULTILINE), (
        f"the `cli` support namespace is missing from root help:\n{out}"
    )
    # restish's stock root commands must NOT be present: their presence means the
    # command surface was never applied (which is what happens when the config
    # file is missing — see ensureConfigFile).
    for stock in ("api", "plugin", "cert", "links", "edit"):
        assert not re.search(rf"^\s+{stock}\s{{2,}}", out, re.MULTILINE), (
            f"stock restish command {stock!r} leaked into the promoted surface, "
            f"which means the baked config did not load:\n{out}"
        )


@pytest.mark.usefixtures("run_sbs")
def test_no_unexpected_restish_mentions_across_surfaces(built_cli, clean_home):
    """The Feature A regression gate, on the real binary (§8.1 #6)."""
    surfaces = {
        "--help": ["--help"],
        "get-skill --help": ["get-skill", "--help"],
        "--version": ["--version"],
        "cli doctor": ["cli", "doctor"],
        "cli config path": ["cli", "config", "path"],
        "unknown command": ["definitely-not-a-command"],
    }
    offenders: dict[str, list[str]] = {}
    for label, args in surfaces.items():
        proc = _run(built_cli, *args, home=clean_home)
        bad = _unexpected_restish_lines(
            proc.stdout + "\n" + proc.stderr, scrub=str(clean_home)
        )
        if bad:
            offenders[label] = bad

    assert not offenders, (
        "unexpected 'restish' mentions in user-visible output.\n"
        + "\n".join(f"  {label}: {lines}" for label, lines in offenders.items())
        + "\n\nIf upstream added a new mention, either it belongs in the §3.3 "
        "inventory (update ALLOWED_RESTISH_SUBSTRINGS here *and* "
        "knownRestishStrings in client/go/tests/main_test.go) or it is a regression."
    )


@pytest.mark.usefixtures("run_sbs")
def test_unknown_command_error_is_branded(built_cli, clean_home):
    proc = _run(built_cli, "definitely-not-a-command", home=clean_home)
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    # The shim produced: unknown command "bogus" for "restish sbs"
    assert "restish sbs" not in combined, combined
    assert "sbs" in combined


@pytest.mark.usefixtures("run_sbs")
def test_doctor_reports_branded_paths(built_cli, clean_home):
    """§4.4: the config dir, cache dir and token cache are all under `sbs`."""
    proc = _run(built_cli, "cli", "doctor", home=clean_home)
    out = proc.stdout + proc.stderr
    for expected in (
        str(clean_home / ".config" / "sbs"),
        str(clean_home / ".cache" / "sbs"),
    ):
        assert expected in out, f"doctor does not report {expected}:\n{out}"
    # The branded config *filename* — the last row of the §3.3 table.
    assert "sbs.json" in out, f"doctor does not report a branded config file:\n{out}"
    assert "restish.json" not in out, (
        f"doctor still reports restish.json; RSH_CONFIG is not taking effect:\n{out}"
    )


# ---------------------------------------------------------------------------
# 2. Zero configuration (M2)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("run_sbs")
def test_list_skills_works_with_no_config_at_all(built_cli, clean_home):
    """M2 as a test: no config file, no `connect`, no prompt, real data out."""
    assert not (clean_home / ".config").exists(), "the fixture HOME must start empty"

    proc = _run(built_cli, "list-skills", home=clean_home)
    assert proc.returncode == 0, (
        f"zero-config invocation failed.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # The store returns a JSON array. Data output must stay byte-exact and
    # parseable — `sbs list-skills | jq` is an explicit goal (§1).
    stripped = proc.stdout.strip()
    assert stripped.startswith("[") or stripped.startswith("{"), (
        f"list-skills did not emit JSON:\n{proc.stdout}"
    )

    # And it configured itself on the way through, rather than demanding a step.
    assert (clean_home / ".config" / "sbs" / "sbs.json").exists(), (
        "the CLI did not create its config file; without one restish fails the "
        "config load and the whole branded surface reverts to stock (see "
        "ensureConfigFile)"
    )


@pytest.mark.usefixtures("run_sbs")
def test_config_file_is_private(built_cli, clean_home):
    """The config can hold a bearer token, and restish refuses a loose one."""
    _run(built_cli, "list-skills", home=clean_home)
    cfg = clean_home / ".config" / "sbs" / "sbs.json"
    assert cfg.exists()
    assert oct(cfg.stat().st_mode)[-3:] == "600", (
        f"config mode is {oct(cfg.stat().st_mode)[-3:]}, want 600"
    )


@pytest.mark.usefixtures("run_sbs")
def test_env_url_overrides_the_baked_slot(built_cli, clean_home, tmp_path):
    """SBS_URL beats the baked slot (§4.1 precedence)."""
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-dead-slot")
    proc = subprocess.run(
        [str(dead), "list-skills"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(clean_home),
            "PATH": os.environ.get("PATH", ""),
            "SBS_URL": STORE_URL,
        },
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"SBS_URL did not override the baked slot.\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# 3. The -ldflags control (§8.2 #12, G8)
# ---------------------------------------------------------------------------


def test_dead_port_build_must_fail(tmp_path, clean_home):
    """The control that makes every other injection claim in this suite mean something.

    If the ``-X …cli.URLSlot`` injection were being ignored — a wrong package
    path is enough — this binary would silently fall
    back to its source default — which in a dev checkout points at a port that
    may well be serving something — and the happy-path tests above would keep
    passing while nothing was actually being injected.
    """
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-dead")
    proc = _run(dead, "list-skills", home=clean_home)

    assert proc.returncode != 0, (
        "a binary baked against a dead port SUCCEEDED, which means -ldflags -X "
        "is not injecting the URL and every 'the URL was baked in' claim in this "
        "suite is unverified. See docs/design/new_cli.md §3.4 #1."
    )
    combined = proc.stdout + proc.stderr
    assert "127.0.0.1:9" in combined, (
        f"the baked URL is not the one being used:\n{combined}"
    )


def test_spec_discovery_failure_is_branded_and_actionable(tmp_path, clean_home):
    """§G3: the offline promoted-root failure names the URL and the three remedies."""
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-offline")
    proc = _run(dead, "list-skills", home=clean_home)
    combined = proc.stdout + proc.stderr

    assert "127.0.0.1:9" in combined, combined
    for remedy in ("sbs connect", "SBS_URL", "sbs download-cli"):
        assert remedy in combined, (
            f"the offline error does not offer {remedy!r}:\n{combined}"
        )


# ---------------------------------------------------------------------------
# Local verbs against a real binary
# ---------------------------------------------------------------------------


def test_connect_writes_config_without_reaching_a_store(tmp_path, clean_home):
    """`connect` must work when the store is unreachable — that is its purpose."""
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-connect")
    proc = _run(dead, "connect", "http://example.test:8080", home=clean_home)

    assert proc.returncode == 0, (
        f"connect failed against an unreachable baked store, but recovering from "
        f"exactly that is what it is for.\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    # Parsed and compared for equality, not searched for a substring: what
    # matters is that `connect` wrote the URL into the field the CLI reads, and a
    # URL appearing anywhere in the file would not establish that.
    cfg = clean_home / ".config" / "sbs" / "sbs.json"
    parsed = json.loads(cfg.read_text(encoding="utf-8"))
    entry = parsed["apis"]["store"]
    assert entry["base_url"] == "http://example.test:8080", parsed
    assert entry["spec_url"] == "http://example.test:8080/openapi.json", parsed


def test_download_cli_works_without_a_reachable_spec(tmp_path, clean_home):
    """`download-cli` must not route through the engine (see localVerb).

    A promoted API cannot build any command until it fetches the spec, so if this
    verb went through Run() it would be unusable against the very store whose
    CLI you are trying to replace.
    """
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-dl")
    proc = _run(dead, "download-cli", home=clean_home)

    # It must fail (nothing is listening) but with a *download* error, not a
    # spec-discovery error — that is the proof it bypassed the engine.
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "spec discovery" not in combined, (
        f"download-cli went through the engine and failed on spec discovery:\n{combined}"
    )
    assert "could not reach" in combined or "127.0.0.1:9" in combined, combined


def test_connect_refuses_an_injection_url(tmp_path, clean_home):
    dead = _build(tmp_path, "http://127.0.0.1:9", name="sbs-inject")
    proc = _run(dead, "connect", 'http://evil.test/"$(id)"', home=clean_home)
    assert proc.returncode == 2, proc.stdout + proc.stderr

    # A bootstrap `{}` config IS expected: ensureConfigFile runs before any verb,
    # because restish fails its whole config load when RSH_CONFIG names a missing
    # file. What must not happen is the refused URL reaching that file.
    cfg = clean_home / ".config" / "sbs" / "sbs.json"
    if cfg.exists():
        body = cfg.read_text(encoding="utf-8")
        assert "evil.test" not in body, f"a refused URL was written to config:\n{body}"
        assert "$(id)" not in body, f"a refused URL was written to config:\n{body}"


@pytest.mark.usefixtures("run_sbs")
def test_login_reports_auth_disabled(built_cli, clean_home):
    """The shim's behaviour, preserved: don't prompt for ignored credentials.

    The e2e store runs with access control disabled, so `login` must say so and
    exit 2 rather than asking for a password the server will ignore.
    """
    proc = _run(built_cli, "login", home=clean_home)
    assert proc.returncode == 2, (
        f"login should exit 2 when auth is disabled.\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "disabled" in (proc.stdout + proc.stderr).lower()
