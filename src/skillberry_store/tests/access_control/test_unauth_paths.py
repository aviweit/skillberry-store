# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""The unauthenticated allow-list — docs/design/new_cli.md §5.5.3, §8.2 #15.

One list decides what is reachable without a session. ``_DEFAULT_UNAUTH_PATHS``
holds it, and ``unauthenticated_paths`` in the config file **adds to** those
defaults rather than replacing them.

That the defaults always apply is what makes them dependable. Every entry in them
is either a probe that must answer before anyone can authenticate, an
authentication endpoint itself, or a public read surface — so a deployment that
lost one would be unable to boot, unable to log in, or unable to serve its own
docs. It is also what makes the CLI download work in every access-control mode
without a second mechanism: ``GET /cli*`` is simply one of the defaults.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from skillberry_store.access_control.config import (
    _DEFAULT_UNAUTH_PATHS,
    _effective_unauth_paths,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

SHIPPED_YAMLS = (
    "access_control_config.yaml",
    "access_control_config.yaml.standalone",
    "access_control_config.yaml.disabled",
)

# What must be reachable without a session whatever the operator wrote.
MUST_REACH = (
    ("GET", "/health"),
    ("GET", "/health/ready"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/whoami"),
    ("GET", "/cli/download"),
    ("HEAD", "/cli/download"),
)


def _write(tmp_path, contents: str) -> str:
    path = tmp_path / "acl.yaml"
    path.write_text(textwrap.dedent(contents))
    return str(path)


def _is_unauth(cfg, method: str, path: str) -> bool:
    """Whether the PEP would let this request through without a session.

    Goes through the config's own ``is_unauthenticated`` — the method the PEP
    actually calls — rather than inspecting the list directly. The entries use a
    ``METHOD /path*`` syntax with trailing-wildcard semantics, so a substring
    assertion over the list would happily pass for an entry that never matches a
    real request.
    """
    return cfg.is_unauthenticated(method, path)


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #


def test_config_entries_extend_the_defaults():
    merged = _effective_unauth_paths(["GET /my-thing"])
    assert merged[: len(_DEFAULT_UNAUTH_PATHS)] == list(_DEFAULT_UNAUTH_PATHS)
    assert "GET /my-thing" in merged


def test_no_config_entries_yields_exactly_the_defaults():
    assert _effective_unauth_paths(None) == list(_DEFAULT_UNAUTH_PATHS)
    assert _effective_unauth_paths([]) == list(_DEFAULT_UNAUTH_PATHS)


def test_re_listing_a_default_is_idempotent():
    """All three shipped files re-list the defaults, so this must not duplicate."""
    merged = _effective_unauth_paths(["GET /health", "GET /cli*"])
    assert merged.count("GET /health") == 1
    assert merged.count("GET /cli*") == 1


# --------------------------------------------------------------------------- #
# §8.2 #15 — whatever the operator writes, the defaults hold
# --------------------------------------------------------------------------- #


def test_a_minimal_operator_list_still_reaches_the_defaults(tmp_path, monkeypatch):
    """A config naming something else entirely must not close the defaults.

    Without this, such a deployment would authenticate its own liveness probe and
    its own login endpoint — an unbootable configuration — as well as the CLI
    download, which has no token to offer.
    """
    monkeypatch.delenv("SBS_SESSION_TTL", raising=False)
    path = _write(
        tmp_path,
        """
        mode: standalone
        unauthenticated_paths:
          - GET /something-unrelated
        standalone:
          users:
            - username: alice
              tenant_id: alice
              password_hash: "$2b$12$hash"
        """,
    )
    cfg = load_config(path)
    assert cfg.mode == "standalone"

    for method, target in MUST_REACH:
        assert _is_unauth(cfg, method, target), (
            f"{method} {target} is not reachable without a session"
        )
    # And the operator's own entry is honoured.
    assert _is_unauth(cfg, "GET", "/something-unrelated")


def test_an_empty_list_still_reaches_the_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("SBS_SESSION_TTL", raising=False)
    path = _write(
        tmp_path,
        """
        mode: standalone
        unauthenticated_paths: []
        standalone:
          users:
            - username: alice
              tenant_id: alice
              password_hash: "$2b$12$hash"
        """,
    )
    cfg = load_config(path)
    for method, target in MUST_REACH:
        assert _is_unauth(cfg, method, target), f"{method} {target}"


def test_defaults_apply_in_disabled_mode(tmp_path):
    path = _write(tmp_path, "mode: disabled\n")
    cfg = load_config(path)
    for method, target in MUST_REACH:
        assert _is_unauth(cfg, method, target), f"{method} {target}"


def test_defaults_apply_with_no_config_file(tmp_path):
    cfg = load_config(str(tmp_path / "does-not-exist.yaml"))
    assert cfg.mode == "disabled"
    for method, target in MUST_REACH:
        assert _is_unauth(cfg, method, target), f"{method} {target}"


def test_added_entries_are_logged(tmp_path, monkeypatch, caplog):
    """What a config opens beyond the defaults should be visible at boot."""
    monkeypatch.delenv("SBS_SESSION_TTL", raising=False)
    path = _write(
        tmp_path,
        """
        mode: standalone
        unauthenticated_paths:
          - GET /extra-public-thing
        standalone:
          users:
            - username: alice
              tenant_id: alice
              password_hash: "$2b$12$hash"
        """,
    )
    with caplog.at_level("INFO"):
        load_config(path)
    assert "GET /extra-public-thing" in caplog.text


def test_no_log_noise_when_a_config_only_re_lists_defaults(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("SBS_SESSION_TTL", raising=False)
    lines = ["mode: disabled", "unauthenticated_paths:"]
    lines += [f"  - {entry}" for entry in _DEFAULT_UNAUTH_PATHS]
    path = tmp_path / "acl.yaml"
    path.write_text("\n".join(lines) + "\n")

    with caplog.at_level("INFO"):
        load_config(str(path))
    assert "adds" not in caplog.text


def test_protected_paths_stay_protected(tmp_path, monkeypatch):
    """The defaults are a floor, not a blanket."""
    monkeypatch.delenv("SBS_SESSION_TTL", raising=False)
    path = _write(
        tmp_path,
        """
        mode: standalone
        standalone:
          users:
            - username: alice
              tenant_id: alice
              password_hash: "$2b$12$hash"
        """,
    )
    cfg = load_config(path)
    for target in ("/skills", "/tools", "/snippets", "/admin/backup", "/plugins"):
        assert not _is_unauth(cfg, "GET", target), f"{target} must require a session"


# --------------------------------------------------------------------------- #
# The default list's own contents
# --------------------------------------------------------------------------- #


def test_cli_download_is_in_the_defaults():
    """This is what makes the download work in every mode, with no second list."""
    assert "GET /cli*" in _DEFAULT_UNAUTH_PATHS
    # HEAD because the route serves both — the audit requires every method on a
    # route to be allow-listed — and because HEAD is how a client reads the
    # artifact's sha256 without transferring it.
    assert "HEAD /cli*" in _DEFAULT_UNAUTH_PATHS


def test_defaults_expose_no_content_or_admin_path():
    for pattern in _DEFAULT_UNAUTH_PATHS:
        for dangerous in ("/skills", "/tools", "/snippets", "/vmcp", "/vnfs"):
            assert dangerous not in pattern, (
                f"the default entry {pattern!r} would expose {dangerous}"
            )


def test_cli_prefix_match_constraint():
    """``GET /cli*`` is a prefix match, so nothing protected may start with `cli`.

    Recorded as a test so the constraint is visible before someone mounts, say,
    `/clients` and finds it public.
    """
    cfg = load_config("does-not-exist.yaml")
    assert _is_unauth(cfg, "GET", "/cli/download")
    assert _is_unauth(cfg, "GET", "/clients"), (
        "This documents a constraint rather than desired behaviour: `GET /cli*` "
        "opens any path starting with `/cli`. Never mount a protected route under "
        "a name beginning with `cli`."
    )
    assert not _is_unauth(cfg, "GET", "/skills")


# --------------------------------------------------------------------------- #
# The shipped YAMLs stay a complete description of the public surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("filename", SHIPPED_YAMLS)
def test_shipped_yaml_lists_the_cli_download(filename):
    path = REPO_ROOT / filename
    if not path.is_file():
        pytest.skip(f"{filename} not present in this checkout")

    listed = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "unauthenticated_paths"
    ) or []
    assert "GET /cli*" in listed, f"{filename} does not list GET /cli*"
    assert "HEAD /cli*" in listed, f"{filename} does not list HEAD /cli*"


@pytest.mark.parametrize("filename", SHIPPED_YAMLS)
def test_shipped_yaml_covers_every_default(filename):
    """The file should read as the whole public surface, not a subset of it."""
    path = REPO_ROOT / filename
    if not path.is_file():
        pytest.skip(f"{filename} not present in this checkout")

    listed = set(
        (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "unauthenticated_paths"
        )
        or []
    )
    missing = [entry for entry in _DEFAULT_UNAUTH_PATHS if entry not in listed]
    assert not missing, (
        f"{filename} omits {missing}. They are still reachable, because the "
        f"defaults always apply — but the file then understates what the "
        f"deployment exposes."
    )
