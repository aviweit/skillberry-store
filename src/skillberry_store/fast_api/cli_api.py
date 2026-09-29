# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Serve the native `sbs` CLI from the store it talks to.

docs/design/new_cli.md §5.5. One endpoint::

    GET, HEAD  /cli/download   ?platform=<goos>-<goarch>  &format=raw|archive

Which variant you get is a query argument, so there is exactly one route, one
cache key shape and one thing to allow-list. It is unauthenticated in every
access-control mode: it is listed in ``_DEFAULT_UNAUTH_PATHS`` alongside
``/health`` and the auth endpoints, and the loader always applies those defaults.
That matters because a browser, ``curl``, CI or a freshly downloaded binary has
no token to offer, and a user who cannot sign in yet is exactly the user who
wants the CLI. The surface serves no tenant data, no configuration and no login
message, and accepts no request body (§7.1).

Because the route carries no ``@requires`` marker, the RBAC audit treats it the
way it treats ``/health``.

**Verification material travels on the response, in headers.** A client that
wants the digest before committing to ~32 MB sends ``HEAD``:

    X-SBS-SHA256   the artifact's sha256, hex — pastes straight into a
                   comparison against `sha256sum` output
    X-SBS-CLI-Version, X-SBS-CLI-Platform, X-SBS-CLI-URL-Injection
    ETag           the same sha256, for caching
    Content-Length the size

Headers rather than a metadata document because the digest then arrives on the
very response that carries the bytes: there is no window between learning the
expected hash and fetching what it describes, and no second round trip.

Two transport choices are load-bearing:

* **``FileResponse``, not ``StreamingResponse``.** Range requests,
  ``Last-Modified`` and conditional GETs come for free, which is what makes a
  32 MB download resumable and a repeat nearly free.
* **``Vary`` on the detection inputs.** Without it, one shared cache hands a
  Windows user the Linux binary (B11).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from skillberry_store.fast_api.platform_detect import (
    SUPPORTED_PLATFORMS,
    VARY_HEADER,
    detect_platform,
    is_supported_platform,
)
from skillberry_store.services.cli_artifacts import (
    STATE_PREPARING,
    STATE_READY,
    CliArtifactService,
)

logger = logging.getLogger(__name__)

# Artifacts are immutable for a given ETag, so a short shared-cache lifetime is
# safe and makes a repeat download nearly free (§7.3). Five minutes rather than a
# year because a deployment's public URL can change and re-prepare the artifact
# under the same path.
_CACHE_CONTROL = "public, max-age=300"

# Per-IP token bucket (§7.3, B12). In-process, and therefore honestly
# per-replica: it stops one careless script saturating a small deployment, and an
# ingress or CDN in front of /cli/download is what bounds it properly.
_RATE_WINDOW_SECONDS = 60

# Budgets per IP per window, by what the request actually costs to serve.
#
# The concern §7.3 names is amplification: an unauthenticated GET ships ~32 MB, so
# 30 of them per minute already allows ~1 GB. A HEAD ships a few hundred bytes of
# headers, so charging it the same is all cost and no protection — and it
# penalises exactly the usage the design encourages, since reading the digest
# before committing to a download means HEAD-then-GET, and polling for readiness
# means repeated HEADs. Bounded generously rather than exempted, because an
# unbounded HEAD flood is still a (much cheaper) nuisance.
_RATE_MAX_BODY_PER_WINDOW = 30
_RATE_MAX_HEAD_PER_WINDOW = 300


class _DownloadLimiter:
    """Per-IP request budget plus a cap on concurrent download starts."""

    def __init__(self, max_concurrent: int):
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, bool], list[float]] = {}
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    def allow(self, client_ip: str, *, sends_body: bool) -> bool:
        """Charge one request against this IP's budget for its kind."""
        limit = _RATE_MAX_BODY_PER_WINDOW if sends_body else _RATE_MAX_HEAD_PER_WINDOW
        key = (client_ip, sends_body)
        now = time.monotonic()
        cutoff = now - _RATE_WINDOW_SECONDS
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            # Opportunistic sweep: this dict otherwise grows once per distinct
            # client IP and never shrinks, which is a slow leak on a public host.
            if len(self._hits) > 4096:
                self._hits = {
                    k: times
                    for k, times in self._hits.items()
                    if any(t > cutoff for t in times)
                }
            return True

    def acquire_slot(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release_slot(self) -> None:
        try:
            self._semaphore.release()
        except ValueError:  # pragma: no cover - defensive
            logger.debug("Download semaphore over-released")


def register_cli_api(
    app: FastAPI,
    service: Optional[CliArtifactService] = None,
    tags: str = "cli",
) -> None:
    """Register the CLI download route, unless ``SBS_CLI_DOWNLOAD`` is off.

    Registration is gated on the flag rather than the handler refusing, so with
    it off the surface genuinely does not exist: ``/cli/download`` is a plain 404,
    no route appears in the app's table, and nothing shows in ``/openapi.json``.
    That is also the documented rollback for this feature (§9).
    """
    if service is None:
        service = getattr(app.state, "cli_artifacts", None)
    if service is None:
        raise RuntimeError("register_cli_api requires a CliArtifactService")

    if not service.settings.enabled:
        logger.info("SBS_CLI_DOWNLOAD is off — /cli/download is not registered")
        return

    limiter = _DownloadLimiter(service.settings.max_concurrent_downloads)
    # Exposed so tests can assert slot bookkeeping without reaching into the
    # closure; nothing in the request path reads it back.
    app.state.cli_download_limiter = limiter

    logger.info(
        "CLI downloads are ON — serving GET /cli/download "
        "(prebuilt=%s, dist=%s, public_url=%s)",
        service.artifacts_dir,
        service.dist_dir,
        service.public_url or "(unset — artifacts stay pristine)",
    )

    def _resolve(
        request: Request, platform: Optional[str], format: str, *, sends_body: bool
    ):
        """Shared by GET and HEAD: returns a Response, or (path, headers, ...)."""
        detection = detect_platform(request.headers, platform)

        # An explicitly requested unknown platform is a 400, never a silent
        # substitution: handing someone a Linux binary because they asked for
        # `darwin-arm64` is worse than telling them no. This is also what makes
        # `platform=../../etc/passwd` a 400 with nothing read (B13, §7.2) — the
        # value is checked against a closed enum before it reaches any path.
        if not is_supported_platform(detection.platform):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "unknown_platform",
                    "platform": detection.platform,
                    "supported": list(SUPPORTED_PLATFORMS),
                },
            )

        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allow(client_ip, sends_body=sends_body):
            return Response(
                status_code=429,
                content='{"detail":"rate_limited"}',
                media_type="application/json",
                headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
            )

        entry = service.resolve(detection.platform)

        if entry.state == STATE_PREPARING:
            # Retry-After instead of queueing: holding the connection open would
            # tie a worker up for a build, and the client can ask again.
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "preparing",
                    "platform": detection.platform,
                    "message": (
                        "This platform's CLI artifact is still being prepared. "
                        "Try again shortly."
                    ),
                },
                headers={"Retry-After": "10", "Vary": VARY_HEADER},
            )

        if entry.state != STATE_READY:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": entry.reason or "unavailable",
                    "platform": detection.platform,
                },
                headers={"Vary": VARY_HEADER},
            )

        if format == "archive":
            # Located from the record rather than recomputed: an empty
            # archive_filename means building it did not succeed, and serving a
            # path derived from the platform would then advertise a file that was
            # never written.
            if not entry.archive_filename or not entry.archive_sha256:
                logger.warning(
                    "No archive recorded for %s; serving raw is still available",
                    detection.platform,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "archive_unavailable",
                        "platform": detection.platform,
                        "message": (
                            "The archive for this platform is not available. "
                            "Request format=raw instead."
                        ),
                    },
                    headers={"Retry-After": "10", "Vary": VARY_HEADER},
                )
            path = service.archive_path(detection.platform)
            filename = entry.archive_filename
            digest = entry.archive_sha256
            media_type = (
                "application/zip"
                if detection.platform.startswith("windows-")
                else "application/gzip"
            )
        else:
            path = service.artifact_path(detection.platform, entry.filename)
            filename = entry.filename
            digest = entry.sha256
            media_type = "application/octet-stream"

        if not path.is_file():
            # The preparation record and the disk disagree: the dist directory
            # was cleaned underneath us, or another replica is mid-write.
            # Truthful 503 — a background re-preparation will fix it.
            logger.warning(
                "Prepared record for %s names %s but the file is missing",
                detection.platform,
                path,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "preparing", "platform": detection.platform},
                headers={"Retry-After": "10", "Vary": VARY_HEADER},
            )

        headers = {
            # The verification material. Hex, so it compares directly against
            # `sha256sum` output, and available from a HEAD without transferring
            # the artifact.
            "X-SBS-SHA256": digest,
            "X-SBS-CLI-Version": service.cli_version,
            "X-SBS-CLI-Platform": detection.platform,
            "X-SBS-CLI-URL-Injection": entry.url_injection,
            # Quoted per RFC 7232; an unquoted ETag is ignored by some caches.
            "ETag": f'"{digest}"',
            "Cache-Control": _CACHE_CONTROL,
            "Vary": VARY_HEADER,
            "X-SBS-Platform-Detection": detection.source,
        }

        # Conditional GET before touching the file: a repeat download of a 32 MB
        # artifact should cost one round trip, not 32 MB (§7.3).
        if_none_match = request.headers.get("if-none-match", "")
        if digest and digest in if_none_match:
            return Response(status_code=304, headers=headers)

        # A HEAD occupies no download capacity, so it neither takes a slot nor
        # can be turned away for want of one.
        holds_slot = False
        if sends_body:
            if not limiter.acquire_slot():
                return Response(
                    status_code=503,
                    content='{"detail":"too_many_downloads"}',
                    media_type="application/json",
                    headers={"Retry-After": "5", **headers},
                )
            holds_slot = True
        try:
            # FileResponse handles Range, If-Range and Last-Modified itself, and
            # sets content-length — which is what makes a HEAD answer identical
            # to the GET's headers with no body (§8.2 #13).
            return FileResponse(
                path=str(path),
                media_type=media_type,
                filename=filename,
                headers=headers,
            )
        finally:
            # Released immediately rather than after the body is sent: Starlette
            # streams the file after this function returns, so holding the slot
            # would require wrapping the response. The cap therefore limits
            # download *starts* per instant, which is what the amplification
            # concern is about.
            #
            # Guarded so acquisition and release stay symmetric. Releasing a slot
            # that was never taken is absorbed — the semaphore is bounded, so it
            # refuses to count past its maximum, and release_slot swallows the
            # resulting ValueError — but then every HEAD would trip that path and
            # its debug log would be routine noise instead of a real signal.
            if holds_slot:
                limiter.release_slot()

    @app.api_route(
        "/cli/download",
        methods=["GET"],
        tags=[tags],
        operation_id="download_cli",
        summary="Download the sbs CLI executable for a platform",
        description=(
            "Returns the native `sbs` executable. The response carries the "
            "artifact's sha256 in `X-SBS-SHA256`, so a client can verify what it "
            "received — send `HEAD` to read it without transferring the file."
        ),
        # No x-mcp-tool: a 32 MB octet-stream is not an agent tool, and the
        # curated-surface audit is where that choice is recorded (§5.5.4).
        openapi_extra={"x-cli-name": "download-cli"},
        response_class=FileResponse,
    )
    async def download_cli(
        request: Request,
        platform: Optional[str] = Query(
            None,
            description=(
                "Artifact platform id (<goos>-<goarch>): "
                + ", ".join(SUPPORTED_PLATFORMS)
                + ". Detected from the request when omitted."
            ),
        ),
        format: str = Query(
            "raw",
            pattern="^(raw|archive)$",
            description=(
                "`raw` for the bare executable, `archive` for a tar.gz/zip that "
                "preserves the executable bit and carries the licence notice."
            ),
        ),
    ):
        return _resolve(request, platform, format, sends_body=True)

    # HEAD on the same path, as a separate registration.
    #
    # It has to exist: the ACL audit requires every method on a route to be
    # allow-listed (the reason /ui lists HEAD), and it is how a client reads the
    # sha256 and the size without transferring the artifact.
    #
    # Separate rather than `methods=["GET", "HEAD"]` because FastAPI emits one
    # OpenAPI operation per method and both would carry operation_id
    # `download_cli` — a duplicate-operation-id warning, an ambiguous generated
    # SDK and an `x-cli-name` collision. Excluded from the schema for the same
    # reason; the GET is the documented operation.
    @app.head("/cli/download", include_in_schema=False)
    async def head_cli_download(
        request: Request,
        platform: Optional[str] = Query(None),
        format: str = Query("raw", pattern="^(raw|archive)$"),
    ):
        return _resolve(request, platform, format, sends_body=False)
