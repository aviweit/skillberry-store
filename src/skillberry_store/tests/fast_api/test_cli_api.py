# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""HTTP behaviour of the /cli/download endpoint.

docs/design/new_cli.md §8.2 #10, #13, #14. Built on a minimal FastAPI app rather
than the full ``SBS``, because these assertions are about one route's status
codes, headers and refusals — constructing the whole store would add the vector
DB, the plugin loader and the object handlers to a test about a
``Content-Disposition`` header.

Registration against the real ``SBS`` app, and the allow-list entry that keeps the
route reachable without a session, are covered in ``test_cli_api_registration.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skillberry_store.fast_api.cli_api import register_cli_api
from skillberry_store.services.cli_artifacts import (
    SLOT_PATTERN,
    CliArtifactService,
    CliArtifactSettings,
)

PUBLIC_URL = "http://store.test:8000"

UA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
UA_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def _fake_prebuilt(root: Path) -> Path:
    """Artifacts shaped like client/go/build.sh's output, with patchable slots."""
    for platform in (
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
    ):
        d = root / platform
        d.mkdir(parents=True)
        name = "sbs.exe" if platform.startswith("windows-") else "sbs"
        (d / name).write_bytes(b"\x7fELF" + SLOT_PATTERN + platform.encode() * 8)
    (root / "LICENSE.restish").write_text("MIT License\n", encoding="utf-8")
    (root / "prebuilt-manifest.json").write_text(
        json.dumps(
            {"cli_version": "1.2.3", "engine": {"name": "restish", "version": "2.3.0"}}
        ),
        encoding="utf-8",
    )
    return root


def _make_app(tmp_path, *, prepare=True, public_url=PUBLIC_URL, enabled=True):
    """A service whose fixed directories are redirected into tmp_path.

    The real locations are module constants beside the CLI source; the constructor
    accepts overrides purely so a test can work in a temporary tree.

    The toolchain lookup is stubbed out, which is both the production default (no
    compiler in the runtime image) and what keeps these tests fast: with a
    toolchain present, `darwin-arm64` is prepared by a real cross-compile, and
    tens of seconds per fixture would make the suite unusable.
    """
    settings = CliArtifactSettings(enabled=enabled)
    service = CliArtifactService(
        settings,
        public_url=public_url,
        cli_commit="gabc123",
        artifacts_dir=_fake_prebuilt(tmp_path / "prebuilt"),
        dist_dir=tmp_path / "dist",
    )
    service._go_binary = lambda: None  # type: ignore[method-assign]
    if prepare:
        service.prepare_all()
    app = FastAPI()
    app.state.cli_artifacts = service
    register_cli_api(app, service=service)
    return app, service


@pytest.fixture
def client_and_service(tmp_path):
    app, service = _make_app(tmp_path)
    with TestClient(app) as client:
        yield client, service


# --------------------------------------------------------------------------- #
# One endpoint, variants by query argument
# --------------------------------------------------------------------------- #


def test_only_the_download_route_is_registered(tmp_path):
    """The whole CLI surface is one path."""
    app, _ = _make_app(tmp_path, prepare=False)
    paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/cli")}
    assert paths == {"/cli/download"}


def test_download_serves_the_prepared_artifact(client_and_service):
    client, service = client_and_service
    resp = client.get("/cli/download?platform=linux-amd64&format=raw")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"

    # Byte-for-byte what preparation produced, which is the strongest statement
    # available: it covers the baked URL, the padding and every other byte, and
    # it is the same digest the response advertises.
    entry = service.resolve("linux-amd64")
    on_disk = service.artifact_path("linux-amd64", entry.filename).read_bytes()
    assert resp.content == on_disk
    assert hashlib.sha256(resp.content).hexdigest() == entry.sha256
    assert SLOT_PATTERN not in resp.content, "an unpatched slot was served"


def test_download_sets_content_disposition(client_and_service):
    client, _ = client_and_service
    disposition = client.get("/cli/download?platform=linux-amd64").headers[
        "content-disposition"
    ]
    assert "attachment" in disposition
    assert 'filename="sbs"' in disposition

    resp = client.get("/cli/download?platform=windows-amd64")
    assert 'filename="sbs.exe"' in resp.headers["content-disposition"]


def test_archive_variant_is_a_tarball(client_and_service):
    client, _ = client_and_service
    resp = client.get("/cli/download?platform=linux-amd64&format=archive")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert "sbs-linux-amd64.tar.gz" in resp.headers["content-disposition"]

    with tarfile.open(fileobj=io.BytesIO(resp.content)) as tf:
        names = tf.getnames()
        assert "sbs" in names
        # A browser download loses the executable bit; the archive preserves it.
        assert tf.getmember("sbs").mode & 0o111
        # We redistribute an MIT binary, so the notice travels with it.
        assert "LICENSE.restish" in names


def test_windows_archive_is_a_zip(client_and_service):
    client, _ = client_and_service
    resp = client.get("/cli/download?platform=windows-amd64&format=archive")
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content[:2] == b"PK"


# --------------------------------------------------------------------------- #
# Verification material in headers
# --------------------------------------------------------------------------- #


def test_response_carries_the_sha256(client_and_service):
    """The digest travels on the response that carries the bytes."""
    client, service = client_and_service
    resp = client.get("/cli/download?platform=linux-amd64")

    entry = service.resolve("linux-amd64")
    assert resp.headers["x-sbs-sha256"] == entry.sha256
    # Hex, so it compares directly against `sha256sum` output.
    assert len(resp.headers["x-sbs-sha256"]) == 64
    assert hashlib.sha256(resp.content).hexdigest() == resp.headers["x-sbs-sha256"]


def test_head_reports_identity_without_a_body(client_and_service):
    """§8.2 #13: how a client learns the digest without transferring 32 MB."""
    client, service = client_and_service
    head = client.head("/cli/download?platform=linux-amd64")
    get = client.get("/cli/download?platform=linux-amd64")

    assert head.status_code == 200
    assert not head.content
    entry = service.resolve("linux-amd64")
    assert head.headers["x-sbs-sha256"] == entry.sha256
    assert head.headers["content-length"] == get.headers["content-length"]
    assert head.headers["etag"] == get.headers["etag"]


def test_headers_describe_the_artifact(client_and_service):
    client, service = client_and_service
    resp = client.head("/cli/download?platform=darwin-arm64")
    assert resp.headers["x-sbs-cli-version"] == "1.2.3"
    assert resp.headers["x-sbs-cli-platform"] == "darwin-arm64"
    # Tells a client whether the binary carries its URL internally.
    assert resp.headers["x-sbs-cli-url-injection"] == (
        service.resolve("darwin-arm64").url_injection
    )


def test_archive_digest_differs_from_the_raw_one(client_and_service):
    """Each variant reports its own digest, or verification would fail."""
    client, _ = client_and_service
    raw = client.head("/cli/download?platform=linux-amd64&format=raw")
    archive = client.head("/cli/download?platform=linux-amd64&format=archive")
    assert raw.headers["x-sbs-sha256"] != archive.headers["x-sbs-sha256"]

    body = client.get("/cli/download?platform=linux-amd64&format=archive").content
    assert hashlib.sha256(body).hexdigest() == archive.headers["x-sbs-sha256"]


# --------------------------------------------------------------------------- #
# Caching and transport
# --------------------------------------------------------------------------- #


def test_caching_headers(client_and_service):
    client, _ = client_and_service
    resp = client.get("/cli/download?platform=linux-amd64")
    assert resp.headers["cache-control"] == "public, max-age=300"
    # Without Vary, one shared cache hands a Windows user the Linux binary (B11).
    for header in ("Sec-CH-UA-Platform", "Sec-CH-UA-Arch", "User-Agent"):
        assert header in resp.headers["vary"]


def test_conditional_get_returns_304(client_and_service):
    """A repeat download of 32 MB should cost one round trip (§7.3)."""
    client, service = client_and_service
    etag = f'"{service.resolve("linux-amd64").sha256}"'
    resp = client.get(
        "/cli/download?platform=linux-amd64", headers={"If-None-Match": etag}
    )
    assert resp.status_code == 304
    assert not resp.content


def test_range_request_returns_206(client_and_service):
    """FileResponse gives resumable downloads — the reason for using it."""
    client, _ = client_and_service
    resp = client.get(
        "/cli/download?platform=linux-amd64", headers={"Range": "bytes=0-9"}
    )
    assert resp.status_code == 206
    assert len(resp.content) == 10
    assert "content-range" in resp.headers


# --------------------------------------------------------------------------- #
# Refusals — §8.2 #10, #14
# --------------------------------------------------------------------------- #


def test_unknown_platform_is_400_not_a_substitution(client_and_service):
    client, _ = client_and_service
    resp = client.get("/cli/download?platform=plan9-mips")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_platform"
    # Naming what is available turns a dead end into a next step.
    assert "linux-amd64" in detail["supported"]


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "linux-amd64/../../../etc/passwd",
        "/etc/passwd",
        "..",
        ".",
    ],
)
def test_traversal_attempts_are_400_and_read_nothing(client_and_service, hostile):
    """§8.2 #14 / B13: the value is checked against a closed enum first."""
    client, _ = client_and_service
    resp = client.get("/cli/download", params={"platform": hostile})
    assert resp.status_code == 400, resp.text
    assert "root:" not in resp.text
    assert "/bin/bash" not in resp.text


def test_bad_format_is_rejected(client_and_service):
    client, _ = client_and_service
    resp = client.get("/cli/download?platform=linux-amd64&format=exe")
    # FastAPI's pattern validation answers 422 for a malformed query value.
    assert resp.status_code == 422


def test_preparing_platform_is_503_with_retry_after(tmp_path):
    """§8.2 #10: Retry-After rather than holding the connection open."""
    app, service = _make_app(tmp_path, prepare=False)
    service.mark_preparing()
    with TestClient(app) as client:
        resp = client.get("/cli/download?platform=linux-amd64")
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "10"
        assert resp.json()["detail"] == "preparing"


def test_unavailable_platform_is_404_with_a_reason(tmp_path):
    app, service = _make_app(tmp_path, prepare=False)
    service.prepare_all()
    service._platforms["windows-amd64"].state = "unavailable"
    service._platforms["windows-amd64"].reason = "not_bundled"
    with TestClient(app) as client:
        resp = client.get("/cli/download?platform=windows-amd64")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not_bundled"


def test_missing_file_is_503(client_and_service):
    """The dist directory was cleaned underneath us: truthful 503, not a 500."""
    client, service = client_and_service
    service.artifact_path("linux-amd64", "sbs").unlink()
    resp = client.get("/cli/download?platform=linux-amd64")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "10"


def _limiter_of(app):
    """The limiter instance the route closed over."""
    from skillberry_store.fast_api import cli_api

    return app.state.cli_download_limiter


def test_rate_limit_returns_429(tmp_path):
    """§8.2 #10 / B12: an unauthenticated 32 MB GET is an amplification risk."""
    app, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        codes = [
            client.get("/cli/download?platform=linux-amd64").status_code
            for _ in range(40)
        ]
    assert 429 in codes, "no request was ever rate limited"
    limited = next(i for i, c in enumerate(codes) if c == 429)
    # Generous enough not to bother a real user chasing a flaky download.
    assert limited >= 20, f"rate limited after only {limited} requests"


# --------------------------------------------------------------------------- #
# Platform detection through the endpoint — §8.2 #11
# --------------------------------------------------------------------------- #


def test_detection_header_reports_the_source(client_and_service):
    client, _ = client_and_service

    explicit = client.get("/cli/download?platform=linux-arm64")
    assert explicit.headers["x-sbs-platform-detection"] == "param"

    hinted = client.get(
        "/cli/download",
        headers={
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Arch": '"x86"',
            "Sec-CH-UA-Bitness": '"64"',
        },
    )
    assert hinted.headers["x-sbs-platform-detection"] == "client-hints"

    # A Mac UA cannot reveal the CPU, so the answer is marked as a guess — which
    # is what makes a wrong one diagnosable from a single response.
    guessed = client.get("/cli/download", headers={"User-Agent": UA_MAC})
    assert guessed.headers["x-sbs-platform-detection"] == "guessed"


def test_detection_picks_the_right_artifact(client_and_service):
    """Not just the right header — the right bytes."""
    client, _ = client_and_service
    windows = client.get("/cli/download", headers={"User-Agent": UA_WINDOWS})
    assert 'filename="sbs.exe"' in windows.headers["content-disposition"]

    mac = client.get("/cli/download", headers={"User-Agent": UA_MAC})
    assert b"darwin-arm64" in mac.content


# --------------------------------------------------------------------------- #
# The off switch — §9 rollback
# --------------------------------------------------------------------------- #


def test_download_off_unregisters_the_route(tmp_path):
    """The route genuinely does not exist, rather than existing and refusing."""
    app, _ = _make_app(tmp_path, prepare=False, enabled=False)
    with TestClient(app) as client:
        assert client.get("/cli/download").status_code == 404
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/cli")]


def test_archive_request_is_refused_when_no_archive_was_built(tmp_path):
    """A 503 naming the alternative, not a 404 for a path nothing wrote.

    The route locates the archive from the preparation record, so an empty record
    has to be answered explicitly — otherwise it would serve a path derived from
    the platform and report a missing file.
    """
    app, service = _make_app(tmp_path)
    entry = service.resolve("linux-amd64")
    entry.archive_filename = ""
    entry.archive_sha256 = ""

    with TestClient(app) as client:
        resp = client.get("/cli/download?platform=linux-amd64&format=archive")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "archive_unavailable"
        assert "format=raw" in resp.json()["message"]

        # The raw variant is unaffected — it is the primary download.
        assert client.get("/cli/download?platform=linux-amd64").status_code == 200


def test_polling_readiness_with_head_is_not_rate_limited(tmp_path):
    """Checking availability must not consume the download budget.

    The limit exists because an unauthenticated GET ships ~32 MB (§7.3). A HEAD
    ships headers, so charging it the same budget protects nothing and breaks the
    two patterns the design asks for: read the digest before committing to a
    download, and poll until a platform is ready. CI hit this — waiting for five
    platforms exhausted 30 requests and the next GET was refused.
    """
    app, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        codes = {
            client.head("/cli/download?platform=linux-amd64").status_code
            for _ in range(120)
        }
        assert codes == {200}, f"a HEAD was refused: {sorted(codes)}"

        # And the download budget is untouched by all that polling.
        assert client.get("/cli/download?platform=linux-amd64").status_code == 200


def test_head_does_not_consume_a_download_slot(tmp_path):
    """The concurrency cap limits bytes in flight, which a HEAD does not produce.

    The count has to come out exact. Over-release is absorbed today (the semaphore
    is bounded and release_slot swallows the ValueError), so a mismatch would not
    surface as an error — it would quietly make the cap mean something other than
    what it says.
    """
    app, service = _make_app(tmp_path)
    limiter = _limiter_of(app)

    with TestClient(app) as client:
        for _ in range(50):
            assert client.head("/cli/download?platform=linux-amd64").status_code == 200

        # Every slot is still free: exhaust them by hand and check the count is
        # exactly the configured maximum, not more (released too often) or
        # fewer (leaked).
        taken = 0
        while limiter.acquire_slot():
            taken += 1
        assert taken == service.settings.max_concurrent_downloads, (
            f"expected {service.settings.max_concurrent_downloads} free slots "
            f"after 50 HEADs, found {taken}"
        )
