"""The CLI docs describe the native download, not a manual restish install.

docs/design/new_cli.md §8.3 #22, in the style of ``test_ui_docs_current.py``.

Docs rot silently, and these particular docs rot into a *dead end*: they used to
tell users to `go install github.com/rest-sh/restish@latest` before `sbs` would
work at all. That instruction now describes a prerequisite that does not exist,
and a user following it would install an unrelated binary and still not have
`sbs`. §9.1 makes updating them part of the change rather than follow-up
paperwork, and this is what enforces that.

The assertions are about *user-actionable content* — the install command, the
breaking change, the platform list — not prose, so ordinary editing does not
break them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CLI_DOC = REPO_ROOT / "docs" / "cli.md"
CLI_PAGE = REPO_ROOT / "site" / "cli.html"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The instructions that told users to install restish by hand. Each is a shape
# that only appears when someone is being asked to obtain restish themselves.
STALE_INSTALL_MARKERS = (
    "go install github.com/rest-sh/restish",
    "github.com/rest-sh/restish/releases",
    "restish to be installed",
    "requires `restish`",
    "requires <code>restish</code>",
)


@pytest.fixture(scope="module")
def cli_doc() -> str:
    assert CLI_DOC.is_file(), f"{CLI_DOC} is missing"
    return CLI_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cli_page() -> str:
    assert CLI_PAGE.is_file(), f"{CLI_PAGE} is missing"
    return CLI_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def changelog() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


@pytest.mark.parametrize("doc_name", ["docs/cli.md", "site/cli.html"])
def test_no_manual_restish_install_instructions(doc_name, cli_doc, cli_page):
    text = cli_doc if doc_name.endswith(".md") else cli_page
    found = [m for m in STALE_INSTALL_MARKERS if m in text]
    assert not found, (
        f"{doc_name} still tells users to install restish by hand: {found}. "
        f"That prerequisite no longer exists — `sbs` is a single static binary "
        f"that embeds restish as a library (docs/design/new_cli.md §4.6)."
    )


@pytest.mark.parametrize("doc_name", ["docs/cli.md", "site/cli.html"])
def test_documents_the_download_endpoint(doc_name, cli_doc, cli_page):
    text = cli_doc if doc_name.endswith(".md") else cli_page
    assert "/cli/download" in text, (
        f"{doc_name} does not document the download endpoint, which is how a user "
        f"gets the CLI."
    )


@pytest.mark.parametrize("doc_name", ["docs/cli.md", "site/cli.html"])
def test_documents_the_pip_replacement_package(doc_name, cli_doc, cli_page):
    """The breaking change is only actionable if the new package is named."""
    text = cli_doc if doc_name.endswith(".md") else cli_page
    assert "skillberry-store-cli" in text, (
        f"{doc_name} does not name skillberry-store-cli. `pip install "
        f"skillberry-store-sdk` no longer provides `sbs`, so a reader needs the "
        f"replacement spelled out."
    )


def test_docs_mention_the_self_download_verbs(cli_doc):
    for verb in ("download-cli", "self-update"):
        assert verb in cli_doc, f"docs/cli.md does not document `sbs {verb}`"


def test_docs_list_the_supported_platforms(cli_doc):
    """A user has to be able to tell whether their machine is covered."""
    for platform in (
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
    ):
        assert platform in cli_doc, f"docs/cli.md does not list {platform}"


def test_docs_cover_the_unsigned_binary_friction(cli_doc):
    """B17: macOS quarantine and SmartScreen are documented, not worked around."""
    assert "quarantine" in cli_doc, (
        "docs/cli.md does not mention macOS quarantine. The binaries are "
        "unsigned, so a browser download is quarantined and the user needs the "
        "remedy."
    )
    assert "com.apple.quarantine" in cli_doc, (
        "give the actual xattr command; 'macOS may complain' is not actionable"
    )


def test_docs_describe_the_branded_config_paths(cli_doc):
    """§4.4: users need to know where config and cache now live."""
    assert "~/.config/sbs" in cli_doc, "docs/cli.md does not give the config directory"
    assert "sbs cli doctor" in cli_doc, (
        "docs/cli.md should point at `sbs cli doctor`, which prints every "
        "resolved path — the answer to most 'where did it put X' questions"
    )


def test_docs_explain_the_config_migration(cli_doc):
    """A user with an existing restish config should learn what happened to it."""
    assert "restish.json" in cli_doc, (
        "docs/cli.md does not mention the one-time migration out of "
        "~/.config/restish/restish.json, so an upgrading user cannot tell whether "
        "their existing registration still applies"
    )


def test_changelog_names_the_breaking_identifier(changelog):
    """§9.1: a deployer must be able to search for the thing that broke.

    ``test_changelog.py`` guards the file's structure; this guards *this*
    migration's searchable identifier, the way that file does for the
    ``:latest-full`` image tag and the removed UI switch.
    """
    assert "sbs console script" in changelog, (
        "the CHANGELOG does not contain the identifier 'sbs console script'. "
        "That is the phrase a deployer whose CI just lost `sbs` would search for."
    )
    assert "skillberry-store-cli" in changelog, (
        "the CHANGELOG must name the replacement package, or the note is not "
        "actionable"
    )
    assert "SDK_PY_CLI" in changelog, (
        "name the flag, so a sibling asset in the shared subtree can tell whether "
        "this affects them"
    )


# --------------------------------------------------------------------------- #
# The configuration surface (§6) is documented where operators look
# --------------------------------------------------------------------------- #

CONFIG_DOC = REPO_ROOT / "docs" / "config-env-vars.md"
CONTAINER_ENV = REPO_ROOT / "container.env"

# Every variable §6 introduces. A knob that exists in code but nowhere in the
# docs is a knob nobody can find, and the deployment defaults file is where an
# operator looks first.
# Every variable the feature introduces. One server-side switch plus the two the
# CLI itself reads. A knob that exists in code but nowhere in the docs is a knob
# nobody can find; a variable in the docs that the code does not read is worse.
CLI_ENV_VARS = (
    "SBS_CLI_DOWNLOAD",
    # Client-side.
    "SBS_URL",
    "SBS_TOKEN",
)

# Variables the design once proposed and the implementation derives instead. The
# docs must not offer them: an operator who sets one would see no effect.
REMOVED_ENV_VARS = (
    "SBS_CLI_PREPARE",
    "SBS_CLI_BUILD_MODE",
    "SBS_CLI_DIST_DIR",
    "SBS_CLI_ARTIFACTS_DIR",
    "SBS_CLI_ARTIFACTS_URL",
    "SBS_CLI_MAX_CONCURRENT_DOWNLOADS",
)


@pytest.mark.parametrize("var", CLI_ENV_VARS)
def test_config_doc_documents_every_cli_env_var(var):
    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert var in text, (
        f"{var} is read by the code but absent from docs/config-env-vars.md"
    )


def test_config_doc_explains_the_unauthenticated_floor():
    """An operator must be able to discover that they cannot close /cli/*."""
    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert "unauthenticated in every" in text.lower(), (
        "docs/config-env-vars.md does not say that /cli/* is unauthenticated in "
        "every mode — a permanently public surface has to be documented as such"
    )
    assert "SBS_CLI_DOWNLOAD=off" in text, (
        "give the operator the way to remove the surface entirely"
    )


def test_config_doc_says_public_url_is_what_gets_baked():
    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert "SBS_PUBLIC_URL" in text
    # Without this connection spelled out, an operator has no reason to set it
    # and their users get binaries needing `sbs connect`.
    assert "baked" in text.lower()


@pytest.mark.parametrize("var", ("SBS_PUBLIC_URL", "SBS_CLI_DOWNLOAD"))
def test_container_env_carries_the_deployment_defaults(var):
    """container.env is baked to /app/.env, so it is the deployment's own doc."""
    text = CONTAINER_ENV.read_text(encoding="utf-8")
    assert var in text, f"{var} is not mentioned in container.env"


@pytest.mark.parametrize("var", REMOVED_ENV_VARS)
def test_docs_offer_no_variable_the_code_ignores(var):
    """A documented setting that does nothing is worse than an undocumented one."""
    for path in (CONFIG_DOC, CONTAINER_ENV, CLI_DOC):
        text = path.read_text(encoding="utf-8")
        assert var not in text, (
            f"{path.name} still documents {var}, which nothing reads. The value is "
            f"derived — see docs/config-env-vars.md for what replaced it."
        )


def test_config_doc_explains_what_is_derived():
    """With one switch, the docs have to say how the rest is decided."""
    text = CONFIG_DOC.read_text(encoding="utf-8")
    assert "derived" in text.lower(), (
        "docs/config-env-vars.md should say that everything besides the switch is "
        "derived, or an operator will go looking for the knobs"
    )
    for topic in ("cli-prebuilt", "cli-dist"):
        assert topic in text, f"the fixed location {topic} is not documented"
