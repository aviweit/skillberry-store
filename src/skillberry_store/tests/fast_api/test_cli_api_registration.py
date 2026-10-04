# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""The /cli/download route on the real ``SBS`` app.

docs/design/new_cli.md §8.2 #15, #17 and §5.5.4. ``test_cli_api.py`` covers the
routes' own behaviour on a minimal app; these assertions need the whole thing,
because what they check is how the CLI surface interacts with the store's global
machinery:

* the **RBAC coverage audit**, which fails startup for any route lacking a
  ``@requires`` marker unless it is allow-listed — so constructing ``SBS`` at all
  is the assertion that these routes are correctly treated as public;
* the **curated MCP surface**, which must not gain a 32 MB octet-stream tool;
* **readiness**, which must not wait for artifact preparation;
* the ``Accept-CH`` advertisement on ``/ui``, without which the download modal
  cannot tell an Apple Silicon Mac from an Intel one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillberry_store.fast_api.platform_detect import CLIENT_HINT_HEADERS


@pytest.fixture(scope="module")
def sbs_app():
    """A real SBS app.

    Constructing it runs ``audit_rbac_coverage``, which raises if any route is
    neither marked with ``@requires`` nor allow-listed. So reaching the yield is
    itself the §8.2 #15 assertion that the /cli routes need no marker — the same
    treatment ``/health`` and ``/admin/metrics`` get.
    """
    from skillberry_store.fast_api.server import SBS

    return SBS()


def test_cli_routes_are_registered(sbs_app):
    paths = {r.path for r in sbs_app.routes if getattr(r, "path", "").startswith("/cli")}
    # The whole CLI surface is one path; variants are query arguments.
    assert paths == {"/cli/download"}


def test_rbac_audit_accepts_the_cli_routes_without_requires(sbs_app):
    """Explicit form of what the fixture already proves.

    These routes deliberately carry no ``@requires``: they are in the mandatory
    unauthenticated floor, so a marker would claim an authorization decision that
    never happens.
    """
    from skillberry_store.access_control.audit import audit_rbac_coverage

    # Must not raise. A missing allow-list entry would surface here as a startup
    # failure naming the offending route.
    audit_rbac_coverage(sbs_app, sbs_app.state.acl_cfg)


def test_acl_config_treats_cli_as_unauthenticated(sbs_app):
    cfg = sbs_app.state.acl_cfg
    for method, path in (
        ("GET", "/cli/download"),
        ("HEAD", "/cli/download"),
    ):
        assert cfg.is_unauthenticated(method, path), f"{method} {path}"


def test_download_is_not_an_mcp_tool(sbs_app):
    """§5.5.4: a 32 MB octet-stream is not an agent tool.

    The curated MCP surface is derived from ``x-mcp-tool`` markers, so this is
    really a check that we did not add one — recorded as a test because the
    curated-surface audit is where that choice is documented.
    """
    operations = sbs_app._mcp_included_operations()
    assert "download_cli" not in operations


def test_download_is_in_the_openapi_spec_with_a_cli_name(sbs_app):
    """The endpoint belongs in the generated SDK and the CLI surface."""
    spec = sbs_app.openapi()
    assert "/cli/download" in spec["paths"]
    assert spec["paths"]["/cli/download"]["get"]["x-cli-name"] == "download-cli"
    # HEAD is excluded from the schema so it cannot collide on operation_id.
    assert "head" not in spec["paths"]["/cli/download"]



def test_no_duplicate_operation_ids(sbs_app):
    """HEAD /cli/download must not collide with the GET's operation id.

    A single `methods=["GET", "HEAD"]` registration emits one OpenAPI operation
    per method sharing one operation_id, which produces a
    duplicate-operation-id warning, an ambiguous generated SDK and a colliding
    `x-cli-name`. The HEAD is therefore a separate, unschema'd registration.
    """
    spec = sbs_app.openapi()
    seen: dict[str, str] = {}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            op_id = operation.get("operationId")
            if not op_id:
                continue
            assert op_id not in seen, (
                f"operationId {op_id!r} is used by both {seen[op_id]} and "
                f"{method.upper()} {path}"
            )
            seen[op_id] = f"{method.upper()} {path}"


def test_cli_artifact_service_is_on_app_state(sbs_app):
    service = sbs_app.state.cli_artifacts
    assert service is not None
    # The public URL is what gets baked into artifacts, so the wiring matters.
    assert service.public_url == sbs_app.settings.public_url


# --------------------------------------------------------------------------- #
# §8.2 #17 — readiness is independent of preparation
# --------------------------------------------------------------------------- #


def test_readiness_has_no_cli_check(sbs_app):
    """Readiness means "can answer content requests" (§5.3, §8.2 #17).

    A store whose CLI download is not prepared yet is fully functional for
    everything else, so gating readiness on it would delay every rollout — and, if
    preparation failed, would keep a healthy store permanently out of the load
    balancer over a download convenience.

    Asserted against the readiness *payload* rather than its status code: the
    endpoint reports 503 until the semantic encoder finishes its background
    warmup, which is a pre-existing condition unrelated to the CLI. What matters
    here is that no CLI-derived check was added to that set.
    """
    with TestClient(sbs_app, raise_server_exceptions=False) as client:
        # Force the not-ready state the test is about.
        sbs_app.state.cli_artifacts.mark_preparing()

        resp = client.get("/health/ready")
        body = resp.json()
        checks = body.get("checks") or body.get("detail", {}).get("checks") or {}
        assert checks, f"could not find the readiness checks in {body!r}"

        cli_keys = [k for k in checks if "cli" in k.lower()]
        assert not cli_keys, (
            f"readiness gained CLI-related check(s) {cli_keys}. Artifact "
            f"preparation must never hold a healthy store out of the load "
            f"balancer (docs/design/new_cli.md §5.3)."
        )

        # And the download route still answers, so a client can poll it without
        # readiness having to be green first. Which status it gives depends on the
        # lifespan's background preparation, which races this test; what matters
        # is that the route is live and not gated on readiness.
        resp = client.head("/cli/download?platform=linux-amd64")
        assert resp.status_code in (200, 404, 503), resp.status_code


def test_download_is_reachable_without_credentials(sbs_app):
    """The whole point: a user who cannot sign in yet can still get the CLI.

    A test checkout has no prepared artifacts, so the answer is a 404 or 503 —
    what matters is that it is not a 401, and that nothing asks for credentials.
    """
    with TestClient(sbs_app) as client:
        resp = client.head("/cli/download?platform=linux-amd64")
        assert resp.status_code != 401
        assert "www-authenticate" not in resp.headers


# --------------------------------------------------------------------------- #
# Accept-CH on /ui (§5.6, B10)
# --------------------------------------------------------------------------- #


def test_ui_advertises_client_hints(sbs_app):
    """Without this the modal cannot distinguish Apple Silicon from Intel.

    Sec-CH-UA-Arch and -Bitness are high-entropy hints that a browser withholds
    until the origin asks, and every Mac User-Agent claims "Intel Mac OS X
    10_15_7" regardless of the real CPU.
    """
    from skillberry_store.fast_api.server import ui_dist_dir

    if not ui_dist_dir().exists():
        pytest.skip("UI bundle not built; /ui is not mounted (run `make ui-build`)")

    with TestClient(sbs_app) as client:
        resp = client.get("/ui/")
        assert resp.status_code == 200
        for header in CLIENT_HINT_HEADERS:
            assert header in resp.headers.get("accept-ch", ""), (
                f"{header} is not advertised on /ui"
            )
        # Critical-CH makes the browser retry the *current* navigation with the
        # hints attached, so the first page load is already accurate.
        for header in CLIENT_HINT_HEADERS:
            assert header in resp.headers.get("critical-ch", "")
