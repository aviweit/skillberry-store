# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Prepare and serve per-deployment `sbs` CLI artifacts.

docs/design/new_cli.md §5.2–§5.4. The job: take the CI-built artifacts that ship
with (or are mounted into) the image, stamp this deployment's own public URL into
them, and record what was prepared so the next start can skip the work.

Two mechanisms, chosen per platform by ``_choose_mechanism``:

``patch``
    Rewrite a fixed-width, ``#``-padded URL slot inside an already-linked binary,
    in place. Milliseconds, zero size delta, and no compiler involved — so it
    covers four of the five platforms whether or not a toolchain is available
    (§5.4 option A, verified as M9).

``rebuild``
    ``GOOS/GOARCH go build -ldflags -X <cli-pkg>.URLSlot=<url>``. Needs a Go
    toolchain and the CLI sources, costs tens of seconds, and is used only for
    ``darwin-arm64`` — Go's linker ad-hoc signs that target, so rewriting its
    bytes breaks execution (B3). Without a toolchain that platform falls back to
    a sidecar file carrying the URL beside the binary.

Everything here runs **off the request path**, in a background task started from
the lifespan hook. Failures downgrade one platform's ``state`` and are logged;
they never fail startup and ``/health/ready`` deliberately does not wait for them
(§5.3) — readiness means "can answer content requests".
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from skillberry_store.fast_api.platform_detect import SUPPORTED_PLATFORMS

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The URL slot
# --------------------------------------------------------------------------- #
# Must match client/go/cli/version.go exactly. The Go side declares
#
#     var URLSlot = "http://localhost:8000#######################################"
#     const SlotWidth = 60
#
# and this module locates that byte sequence in a linked binary and overwrites
# it. The two constants are a contract between a Python writer and a Go reader,
# so `test_cli_artifacts_service.py` reads them back out of the Go source and
# asserts they agree — a silent drift here produces a binary that points at the
# wrong store, which is the single worst failure this feature can have.
SLOT_WIDTH = 60
SLOT_PAD = b"#"

# The Go package holding the `-ldflags -X` targets, and the package to build.
#
# Both are coupled to client/go's layout: the implementation is the importable
# `cli` package and the binary is `cli/cmd/sbs`. Named constants rather than
# inline strings because a wrong `-X` package path is silently ignored by the
# linker — the build succeeds and nothing is injected.
GO_LDFLAGS_PKG = "github.com/skillberry-ai/skillberry-store/client/go/cli"
GO_CMD_PKG = "./cli/cmd/sbs"
SLOT_DEFAULT_URL = b"http://localhost:8000"
SLOT_PATTERN = SLOT_DEFAULT_URL + SLOT_PAD * (SLOT_WIDTH - len(SLOT_DEFAULT_URL))

# §5.7 / §7.2 / B14. Identical to the Go and shell validators. A URL from this
# grammar cannot carry a quote, a backtick, a dollar, a semicolon, a pipe or
# whitespace, which is what makes it safe to inline into a generated shell script
# and to pass as an -ldflags value.
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$")

# State values in the served manifest (§5.5.1).
STATE_READY = "ready"
STATE_PREPARING = "preparing"
STATE_UNAVAILABLE = "unavailable"

# Reasons a platform can be unavailable. Surfaced verbatim to the UI and the CLI,
# so each has to mean something to someone reading it in a terminal.
REASON_NOT_BUNDLED = "not_bundled"
REASON_PREPARE_FAILED = "prepare_failed"
REASON_URL_TOO_LONG = "url_too_long"
REASON_NO_SLOT = "no_slot_found"
REASON_NO_TOOLCHAIN = "no_toolchain"

# Mechanisms recorded per artifact.
MECHANISM_PATCH = "patch"
MECHANISM_REBUILD = "rebuild"
MECHANISM_SIDECAR = "sidecar"
MECHANISM_PRISTINE = "pristine"


class UnacceptableURL(ValueError):
    """A URL that must never reach a linker flag, a script or a binary."""


def validate_public_url(url: str, *, for_slot: bool = False) -> str:
    """Validate and normalise a URL before it is baked, patched or inlined.

    ``for_slot`` additionally enforces the fixed slot width, which only applies
    to a value being written into a binary.

    This is the single choke point named in §7.2. A value like
    ``evil.com/"$(id)"`` reaching a generated shell script is command execution
    on the user's machine, and reaching an ``-ldflags`` value is argument
    injection into our own build. Both are prevented here rather than by quoting
    at each use site, because a use site that forgets is silent.
    """
    if not url or not isinstance(url, str):
        raise UnacceptableURL("URL is empty")
    candidate = url.strip().rstrip("/")
    if not URL_RE.match(candidate):
        raise UnacceptableURL(f"{url!r} is not an acceptable http(s) URL")
    if for_slot and len(candidate.encode()) > SLOT_WIDTH:
        raise UnacceptableURL(
            f"{candidate!r} is {len(candidate.encode())} bytes, which exceeds the "
            f"{SLOT_WIDTH}-byte slot"
        )
    return candidate


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Streaming sha256 — these files are ~32 MB, so never read one whole."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Slot patching
# --------------------------------------------------------------------------- #


def patch_url_slot(data: bytearray, url: str) -> int:
    """Overwrite every copy of the URL slot in ``data``. Returns the count.

    Operates on a mutable buffer rather than a file so the caller controls
    atomicity, and so this is trivially testable.

    Two invariants, both asserted rather than assumed:

    * The replacement is exactly ``SLOT_WIDTH`` bytes, so the file size cannot
      change. A linked binary has offsets and, on some formats, checksums that a
      length change would invalidate.
    * The padding byte cannot appear in a URL that passes ``validate_public_url``,
      so trimming it on the Go side can never truncate a real URL.
    """
    encoded = validate_public_url(url, for_slot=True).encode()
    replacement = encoded + SLOT_PAD * (SLOT_WIDTH - len(encoded))
    if len(replacement) != SLOT_WIDTH:  # pragma: no cover - arithmetic guard
        raise UnacceptableURL(
            f"internal error: replacement is {len(replacement)} bytes, want {SLOT_WIDTH}"
        )

    count = 0
    start = 0
    while True:
        offset = data.find(SLOT_PATTERN, start)
        if offset < 0:
            break
        data[offset : offset + SLOT_WIDTH] = replacement
        count += 1
        start = offset + SLOT_WIDTH
    return count


# --------------------------------------------------------------------------- #
# Manifest records
# --------------------------------------------------------------------------- #


@dataclass
class PlatformArtifact:
    """One platform's entry in the served manifest."""

    platform: str
    state: str = STATE_UNAVAILABLE
    filename: str = ""
    size: int = 0
    sha256: str = ""
    url_injection: str = ""
    reason: str = ""
    stamp: str = ""
    archive_filename: str = ""
    archive_size: int = 0
    archive_sha256: str = ""

    def to_manifest(self) -> dict:
        """Render this platform's record for the on-disk preparation manifest."""
        entry: dict = {"state": self.state}
        if self.state == STATE_READY:
            entry.update(
                {
                    "filename": self.filename,
                    "size": self.size,
                    "sha256": self.sha256,
                    "url_injection": self.url_injection,
                }
            )
            if self.archive_sha256:
                entry["archive_sha256"] = self.archive_sha256
                entry["archive_size"] = self.archive_size
                entry["archive_filename"] = self.archive_filename
        else:
            if self.reason:
                entry["reason"] = self.reason
            if self.state == STATE_PREPARING:
                entry["retry_after"] = 10
        return entry


# --------------------------------------------------------------------------- #
# Fixed locations
# --------------------------------------------------------------------------- #
# Both directories sit at the top of the tree, and neither is configurable: the
# prepared artifacts are a function of the source tree and the deployment's
# public URL, so there is nothing for an operator to choose (R5).
#
#   cli-prebuilt/  the cross-compiled binaries `client/go/build.sh` emits, baked
#                  into the image or mounted in
#   cli-dist/      the same binaries with this deployment's URL stamped into
#                  them, plus the manifest that records what was prepared
#
# Both are build/cache products and are gitignored.
#
# `cli-prebuilt` is not a name chosen here -- it is where the build already
# writes, and the only reason this constant has to agree with something. One
# spelling has to be canonical and the build's is the one with five other
# users: build.sh's own --out default, CLI_PREBUILT in .mk/dev.mk, the
# `cli-prebuilt` artifact in the CI workflow, and both Dockerfile stages
# (/app/cli-prebuilt, which is what this resolves to in the image). This used
# to read client/go/cli/{prebuilt,dist} instead, a path nothing wrote to, so a
# local `make cli-dist` was invisible to the server and every platform reported
# `not_bundled`; CI passed only because its smoke job hand-copied the artifacts
# across. test_artifacts_dir_is_where_the_build_writes pins the two together.
_REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS_DIR = _REPO_ROOT / "cli-prebuilt"
DIST_DIR = _REPO_ROOT / "cli-dist"

# Concurrent downloads allowed to start at once (§7.3). An unauthenticated
# ~32 MB GET is an amplification opportunity; this is in-process and therefore
# per-replica, so an ingress or CDN in front of /cli/download is what actually
# bounds it.
MAX_CONCURRENT_DOWNLOADS = 8


@dataclass
class CliArtifactSettings:
    """Whether this store serves its CLI.

    One switch, ``SBS_CLI_DOWNLOAD``. Everything else that preparing and serving
    an artifact needs is derived: the directories are fixed beside the CLI source
    (``ARTIFACTS_DIR`` / ``DIST_DIR``), the injection mechanism is chosen per
    platform from what the runtime can actually do (``_choose_mechanism``), and
    preparation runs exactly when the feature is on.
    """

    enabled: bool = True

    @classmethod
    def from_env(cls) -> "CliArtifactSettings":
        return cls(enabled=_env_flag("SBS_CLI_DOWNLOAD", default=True))

    @property
    def max_concurrent_downloads(self) -> int:
        return MAX_CONCURRENT_DOWNLOADS


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    # `off` is the documented value; `false`/`0`/`no` are accepted because an
    # operator who writes one of those plainly means the same thing.
    return raw.strip().lower() not in ("off", "false", "0", "no", "")


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class CliArtifactService:
    """Owns the prepared-artifact directory and the manifest that describes it.

    Lifecycle:

    * ``__init__`` is cheap and does no I/O, so constructing it in ``SBS.__init__``
      cannot slow startup or fail it.
    * ``load_manifest`` reads whatever a previous run (or another replica) left
      behind, so downloads can be served immediately on a warm dist dir.
    * ``prepare_all`` is the background task. It is the only method that writes.
    * ``resolve`` answers a download request from in-memory state.
    """

    #: Marker file recording the inputs a prepared artifact was made from.
    MANIFEST_NAME = "manifest.json"

    def __init__(
        self,
        settings: CliArtifactSettings,
        *,
        public_url: Optional[str] = None,
        cli_commit: str = "unknown",
        dist_dir: Optional[Path] = None,
        artifacts_dir: Optional[Path] = None,
    ):
        # `dist_dir` and `artifacts_dir` default to the fixed locations beside the
        # CLI source and exist as parameters only so tests can point a service at
        # a temporary tree. They are not a configuration surface: nothing reads
        # them from the environment, and the server never passes them.
        self.settings = settings
        self._dist_dir = Path(dist_dir) if dist_dir else DIST_DIR
        self._artifacts_dir = Path(artifacts_dir) if artifacts_dir else ARTIFACTS_DIR
        self.public_url = public_url
        self.cli_commit = cli_commit
        self._platforms: dict[str, PlatformArtifact] = {
            platform: PlatformArtifact(platform=platform, reason=REASON_NOT_BUNDLED)
            for platform in SUPPORTED_PLATFORMS
        }
        self._cli_version = "unknown"
        self._engine_version = "unknown"
        self._generated_at: Optional[str] = None
        self._prepared = False

    # -- properties -------------------------------------------------------- #

    @property
    def dist_dir(self) -> Path:
        return self._dist_dir

    @property
    def artifacts_dir(self) -> Path:
        return self._artifacts_dir

    @property
    def cli_version(self) -> str:
        return self._cli_version

    @property
    def engine_version(self) -> str:
        return self._engine_version

    # -- stamping ---------------------------------------------------------- #

    def stamp_key(self, platform: str, mechanism: str) -> str:
        """The §5.3 preparation stamp.

        ``sha256(public_url ‖ cli_commit ‖ engine_version ‖ platform ‖ mechanism)``

        Every input is something that, if changed, makes the prepared artifact
        wrong rather than merely stale:

        * ``public_url`` — the whole point; a changed URL means a re-prepare.
        * ``cli_commit`` — new CLI code means a new artifact.
        * ``engine_version`` — a restish upgrade changes behaviour users see.
        * ``platform`` — separate artifacts, separate stamps.
        * ``mechanism`` — a ``patch`` artifact and a ``rebuild`` artifact for the
          same URL are different bytes, and the manifest advertises which.

        The stamp is checked *together with* the file's recorded size and sha256
        (see ``_is_current``), so a corrupt or truncated file re-prepares even
        when every input is unchanged.
        """
        material = "\u0000".join(
            [
                self.public_url or "",
                self.cli_commit,
                self._engine_version,
                platform,
                mechanism,
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def _is_current(self, entry: PlatformArtifact, mechanism: str) -> bool:
        """Whether ``entry`` can be reused without doing any work."""
        if entry.state != STATE_READY or not entry.stamp:
            return False
        if entry.stamp != self.stamp_key(entry.platform, mechanism):
            return False
        path = self.artifact_path(entry.platform, entry.filename)
        if not path.is_file():
            return False
        try:
            if path.stat().st_size != entry.size:
                return False
        except OSError:
            return False
        # The sha256 re-check is what turns "the stamp matches" into "the file is
        # actually the one the stamp describes". It costs ~30 ms for 32 MB and
        # runs once per platform at startup, which is a fair price for not
        # serving a half-written artifact after an unclean shutdown.
        return sha256_file(path) == entry.sha256

    # -- paths ------------------------------------------------------------- #

    def artifact_path(self, platform: str, filename: str = "") -> Path:
        """Where a prepared artifact lives.

        ``platform`` is always a member of the closed ``SUPPORTED_PLATFORMS``
        enum by the time it reaches here — callers validate first — so this join
        cannot be a traversal (B13, §7.2). The belt-and-braces check below makes
        that a property of this function rather than of every caller.
        """
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unknown platform {platform!r}")
        name = filename or self.default_filename(platform)
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError(f"unsafe artifact filename {name!r}")
        return self.dist_dir / platform / name

    @staticmethod
    def default_filename(platform: str) -> str:
        return "sbs.exe" if platform.startswith("windows-") else "sbs"

    @staticmethod
    def archive_name(platform: str) -> str:
        suffix = ".zip" if platform.startswith("windows-") else ".tar.gz"
        return f"sbs-{platform}{suffix}"

    def archive_path(self, platform: str) -> Path:
        return self.dist_dir / platform / self.archive_name(platform)

    # -- manifest I/O ------------------------------------------------------ #

    def manifest_path(self) -> Path:
        return self.dist_dir / self.MANIFEST_NAME

    def load_manifest(self) -> None:
        """Adopt a manifest a previous run or another replica wrote.

        Best-effort by design: the dist dir is a *cache* (§5.3, B6). A missing,
        truncated or unparseable manifest means "nothing prepared yet", which
        costs one background re-preparation and never an error to a user.
        """
        path = self.manifest_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return

        # A manifest prepared for a *different* public URL must not be adopted:
        # its artifacts point somewhere else. Discarding it here is what makes a
        # changed SBS_PUBLIC_URL re-prepare rather than silently serve stale
        # binaries — the stamp check would catch it too, but only after this
        # method had already advertised them as ready.
        if raw.get("public_url") and raw.get("public_url") != self.public_url:
            logger.info(
                "Ignoring prepared CLI manifest for %s; this deployment serves %s",
                raw.get("public_url"),
                self.public_url,
            )
            return

        self._cli_version = raw.get("cli_version") or self._cli_version
        engine = raw.get("engine") or {}
        if isinstance(engine, dict) and engine.get("version"):
            self._engine_version = engine["version"]
        self._generated_at = raw.get("generated_at")

        for platform, entry in (raw.get("platforms") or {}).items():
            if platform not in self._platforms or not isinstance(entry, dict):
                continue
            self._platforms[platform] = PlatformArtifact(
                platform=platform,
                state=entry.get("state", STATE_UNAVAILABLE),
                filename=entry.get("filename", ""),
                size=int(entry.get("size") or 0),
                sha256=entry.get("sha256", ""),
                url_injection=entry.get("url_injection", ""),
                reason=entry.get("reason", ""),
                stamp=entry.get("stamp", ""),
                archive_filename=entry.get("archive_filename", ""),
                archive_size=int(entry.get("archive_size") or 0),
                archive_sha256=entry.get("archive_sha256", ""),
            )

    def manifest(self) -> dict:
        """The preparation manifest, as persisted in the dist directory.

        Internal state, not a served document: ``_is_current`` reads it back on
        the next start to decide whether any platform needs re-preparing, and it
        is what lets two replicas sharing a volume agree on what is already done.
        Clients learn an artifact's identity from the response headers on
        ``/cli/download`` instead.
        """
        return {
            "cli_name": "sbs",
            "cli_version": self._cli_version,
            "public_url": self.public_url,
            "generated_at": self._generated_at,
            "engine": {
                "name": "restish",
                "version": self._engine_version,
                "license": "MIT",
            },
            "platforms": {
                platform: entry.to_manifest()
                for platform, entry in sorted(self._platforms.items())
            },
        }

    def _write_manifest(self) -> None:
        """Write the manifest last, atomically (§5.3, B7).

        Last, because it is what advertises the artifacts: a manifest that named
        a file still being written would let a reader download a truncated
        binary. Atomically, because two replicas can each prepare their own copy
        and a torn manifest would make both unreadable. The ETag is the content
        sha256, so independent copies still agree.
        """
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        doc = self.manifest()
        # The stamp is persisted but deliberately NOT part of the served
        # document: it is an internal cache key, and publishing it would invite
        # clients to depend on its shape.
        for platform, entry in self._platforms.items():
            if entry.stamp:
                doc["platforms"][platform]["stamp"] = entry.stamp

        target = self.manifest_path()
        fd, tmp_name = tempfile.mkstemp(dir=str(self.dist_dir), prefix=".manifest-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            # Leaving a .manifest-* turd behind would accumulate on every failure.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- resolution (request path) ----------------------------------------- #

    def resolve(self, platform: str) -> PlatformArtifact:
        """The in-memory entry for a platform. No I/O — this is on the hot path."""
        return self._platforms.get(
            platform, PlatformArtifact(platform=platform, reason=REASON_NOT_BUNDLED)
        )

    def mark_preparing(self) -> None:
        """Flip not-yet-ready platforms to `preparing` before the work starts.

        So a client polling during startup gets `503 + Retry-After` and a
        "being prepared" message, rather than `unavailable` — which reads as
        "never coming" and would send a user looking for a different install
        method (§5.5.1, §5.8 #2).
        """
        for entry in self._platforms.values():
            if entry.state != STATE_READY:
                entry.state = STATE_PREPARING
                entry.reason = ""

    # -- preparation (background) ------------------------------------------ #

    def prepare_all(self) -> None:
        """Prepare an artifact per platform for this deployment's public URL.

        This is the background task of §5.3. It is synchronous and blocking by
        design — the caller runs it in a thread executor — because the work is
        file I/O and subprocesses, neither of which benefits from async.

        Every failure mode degrades one platform and is logged. Nothing raised
        from here should reach the caller, because the caller is a fire-and-forget
        task whose failure would be an unhandled exception in the event loop.
        """
        if not self.settings.enabled:
            logger.info("SBS_CLI_DOWNLOAD is off; not preparing CLI artifacts")
            return

        if not self.public_url:
            # Without a public URL there is nothing to inject. Serving the
            # CI-built artifacts pristine is the truthful outcome: they carry
            # their compile-time default, the manifest says `pristine`, and the
            # install script (which derives the URL per request) still works.
            logger.warning(
                "SBS_PUBLIC_URL is not set; serving pristine CLI artifacts that "
                "carry their compiled-in default URL. Downloaded binaries will "
                "need `sbs connect <url>`."
            )

        started = time.monotonic()
        source_dir = self.artifacts_dir
        self._read_prebuilt_metadata(source_dir)

        if not source_dir.is_dir():
            logger.warning(
                "No CLI artifacts at %s; every platform will report %r. Build "
                "them with `make cli-dist`, or mount them there. The location is "
                "derived, not configurable (R5), so there is no variable to set.",
                source_dir,
                REASON_NOT_BUNDLED,
            )
            for entry in self._platforms.values():
                if entry.state != STATE_READY:
                    entry.state = STATE_UNAVAILABLE
                    entry.reason = REASON_NOT_BUNDLED
            self._safe_write_manifest()
            return

        # One inter-process lock around the whole dist dir (§5.3, B7). Two
        # workers or two replicas sharing a volume must not interleave writes to
        # the same artifact, and the lock is cheaper than making every individual
        # write safe against a concurrent writer.
        lock = self._dist_lock()
        acquired = lock.acquire(timeout=120) if lock else True
        if not acquired:
            logger.warning(
                "Could not acquire the CLI dist lock within 120s; another process "
                "is preparing artifacts. Skipping this run."
            )
            return
        try:
            for platform in SUPPORTED_PLATFORMS:
                try:
                    self._prepare_platform(platform, source_dir)
                except Exception:
                    # One platform's failure must not abandon the others.
                    logger.exception("Preparing the %s CLI artifact failed", platform)
                    entry = self._platforms[platform]
                    entry.state = STATE_UNAVAILABLE
                    entry.reason = REASON_PREPARE_FAILED
            self._safe_write_manifest()
        finally:
            if lock:
                try:
                    lock.release()
                except Exception:  # pragma: no cover - lock teardown
                    logger.debug("Releasing the CLI dist lock failed", exc_info=True)

        self._prepared = True
        ready = sorted(p for p, e in self._platforms.items() if e.state == STATE_READY)
        logger.info(
            "CLI artifact preparation finished in %.2fs: %d/%d ready (%s)",
            time.monotonic() - started,
            len(ready),
            len(SUPPORTED_PLATFORMS),
            ", ".join(ready) if ready else "none",
        )

    def _dist_lock(self):
        """An inter-process lock file beside the dist dir.

        Returns None when fasteners is unavailable rather than failing: a
        single-process deployment does not need it, and the degradation
        (a possible duplicated preparation) is harmless because each write is
        atomic anyway.
        """
        try:
            import fasteners
        except ImportError:  # pragma: no cover - fasteners is a declared dep
            return None
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        return fasteners.InterProcessLock(str(self.dist_dir / ".prepare.lock"))

    def _safe_write_manifest(self) -> None:
        # Stamped at write time rather than at construction: the field answers
        # "when were these artifacts prepared?", which is what a support request
        # or a stale-cache investigation actually asks. Setting it in __init__
        # would report process start and be wrong for an adopted manifest.
        self._generated_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        try:
            self._write_manifest()
        except OSError:
            # A read-only dist dir is a misconfiguration worth logging, but the
            # in-memory manifest is still correct and still served.
            logger.exception("Could not write the CLI manifest to %s", self.dist_dir)

    def _read_prebuilt_metadata(self, source_dir: Path) -> None:
        """Adopt cli_version and engine version from the CI build's manifest.

        These are inputs to the stamp key, so they have to come from the artifacts
        themselves rather than from the running server's own version — the image
        can carry artifacts built from a different commit than the Python code.
        """
        path = source_dir / "prebuilt-manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self._cli_version = raw.get("cli_version") or self._cli_version
        engine = raw.get("engine") or {}
        if isinstance(engine, dict) and engine.get("version"):
            self._engine_version = engine["version"]

    def _source_artifact(self, source_dir: Path, platform: str) -> Optional[Path]:
        """Locate the CI-built artifact for a platform.

        Two layouts are accepted: ``<dir>/<platform>/sbs`` (what client/go/build.sh
        emits) and a flat ``<dir>/sbs-<platform>``, because a release-asset
        download naturally produces the latter and making operators rename files
        would be a pointless obstacle.
        """
        filename = self.default_filename(platform)
        candidates = [
            source_dir / platform / filename,
            source_dir / f"sbs-{platform}" / filename,
            source_dir / f"sbs-{platform}",
            source_dir / f"sbs-{platform}.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _choose_mechanism(self, platform: str) -> str:
        """Which injection mechanism applies to this platform (§5.2).

        The table this implements:

        ==================  =================  =================
        Platform            Toolchain          No toolchain
        ==================  =================  =================
        linux-amd64         patch              patch
        linux-arm64         patch              patch
        darwin-amd64        patch              patch
        windows-amd64       patch              patch
        darwin-arm64        **rebuild**        **sidecar**
        ==================  =================  =================

        The cheapest mechanism that works, per platform. ``patch`` rewrites a
        fixed-width slot in an already-linked binary: milliseconds, zero size
        delta, and no compiler involved. It covers four of the five platforms, so
        those four never need a toolchain even when one is available — a
        cross-compile would cost tens of seconds per platform to produce a binary
        indistinguishable from the patched one.

        ``darwin-arm64`` is the exception, and the only reason a toolchain earns
        its keep here: Go's linker ad-hoc signs that target, so rewriting bytes
        inside the signed image invalidates the signature and macOS refuses to
        exec it (B3). With a toolchain that cell is a real ``rebuild``; without
        one it falls back to a sidecar file carrying the URL beside the binary.

        No configuration: the choice follows from the platform and from whether a
        toolchain is present.
        """
        if not self.public_url:
            return MECHANISM_PRISTINE
        if platform == "darwin-arm64":
            return MECHANISM_REBUILD if self._can_rebuild() else MECHANISM_SIDECAR
        return MECHANISM_PATCH

    def _go_binary(self) -> Optional[str]:
        """Locate a Go toolchain, or None.

        Nothing here assumes an install layout: Go arrives differently on a
        developer laptop, a CI runner and a container image. ``PATH`` first, then
        ``GOROOT/bin/go`` for the common case of GOROOT set without its bin
        directory exported, then the conventional install locations.

        Discovery, not configuration — there is no knob to set. A deployment that
        has a toolchain gets real per-platform rebuilds; one that does not gets
        in-place slot patching, which is the default and needs no compiler.
        """
        found = shutil.which("go")
        if found:
            return found

        goroot = os.environ.get("GOROOT")
        if goroot:
            candidate = Path(goroot) / "bin" / "go"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

        home = Path.home()
        for candidate in (
            Path("/usr/local/go/bin/go"),
            Path("/opt/go/bin/go"),
            Path("/opt/homebrew/bin/go"),
            home / ".local" / "go" / "bin" / "go",
            home / "go" / "bin" / "go",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def _have_toolchain(self) -> bool:
        return self._go_binary() is not None

    def _go_module_root(self) -> Path:
        """The Go module root, which is where a build must run from."""
        return Path(__file__).resolve().parents[3] / "client" / "go"

    def _can_rebuild(self) -> bool:
        """Whether a real per-URL build is possible here.

        Needs both a toolchain and the CLI sources. An image can carry the
        prebuilt artifacts without the Go module — there is no reason to ship
        source to a runtime that is only serving binaries — and in that case the
        platform falls back to a sidecar rather than reporting itself unavailable.
        """
        return self._have_toolchain() and (self._go_module_root() / "go.mod").is_file()

    def _prepare_platform(self, platform: str, source_dir: Path) -> None:
        """Prepare one platform's artifact, or record why it could not be."""
        entry = self._platforms[platform]
        mechanism = self._choose_mechanism(platform)

        # The stamp check (§5.3): unchanged inputs means literally zero work,
        # which is what makes a restart with an unchanged SBS_PUBLIC_URL free.
        if self._is_current(entry, mechanism):
            logger.debug("CLI artifact for %s is already current; skipping", platform)
            return

        source = self._source_artifact(source_dir, platform)
        if source is None:
            entry.state = STATE_UNAVAILABLE
            entry.reason = REASON_NOT_BUNDLED
            logger.info(
                "No prebuilt CLI artifact for %s under %s", platform, source_dir
            )
            return

        filename = self.default_filename(platform)
        target = self.artifact_path(platform, filename)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            if mechanism == MECHANISM_REBUILD:
                self._rebuild(platform, target)
            else:
                self._copy_and_maybe_patch(platform, source, target, mechanism)
        except UnacceptableURL as exc:
            entry.state = STATE_UNAVAILABLE
            entry.reason = (
                REASON_URL_TOO_LONG if "exceeds" in str(exc) else REASON_PREPARE_FAILED
            )
            logger.error("Refusing to prepare %s: %s", platform, exc)
            return
        except _NoSlotFound:
            # A CI artifact with no slot is a build/version mismatch: the Python
            # SLOT_PATTERN and the Go urlSlot have drifted. Serving it pristine
            # would point users at the wrong store, so refuse instead.
            entry.state = STATE_UNAVAILABLE
            entry.reason = REASON_NO_SLOT
            logger.error(
                "The %s artifact contains no URL slot. The prebuilt binary and "
                "this server disagree about client/go/cli/version.go's URLSlot. "
                "Rebuild "
                "the artifacts with `make cli-dist`.",
                platform,
            )
            return
        except _NoToolchain:
            entry.state = STATE_UNAVAILABLE
            entry.reason = REASON_NO_TOOLCHAIN
            logger.error(
                "Preparing the %s artifact needs a Go toolchain, which this "
                "image does not have. Without one the platform falls back to a "
                "sidecar file carrying the store URL; add a toolchain to get a "
                "real per-URL build.",
                platform,
            )
            return

        sidecar_note = ""
        if mechanism == MECHANISM_SIDECAR:
            # The URL lives beside the binary rather than inside it, so the
            # archive is the only complete download for this platform.
            self._write_sidecar(platform)
            sidecar_note = " (URL in a sidecar file; use format=archive)"

        # sha256 is computed AFTER patching (§7.2), so the record describes the
        # bytes actually served rather than the bytes CI produced.
        entry.filename = filename
        entry.size = target.stat().st_size
        entry.sha256 = sha256_file(target)
        entry.url_injection = mechanism
        entry.stamp = self.stamp_key(platform, mechanism)
        entry.reason = ""

        self._build_archive(platform, entry)

        # READY last, once every variant this platform offers is on disk.
        #
        # `state` is what the download route reads, so setting it before the
        # archive exists advertises `format=archive` while it is still being
        # written — gzipping a 33 MB binary is not instant, and a client that asks
        # inside that window gets a 503 for an artifact the record already claims
        # is ready. Publishing the state last makes "ready" mean ready for every
        # variant, which is the same discipline that writes the manifest last.
        entry.state = STATE_READY

        logger.info(
            "Prepared the %s CLI artifact via %s: %d bytes, sha256 %s%s",
            platform,
            mechanism,
            entry.size,
            entry.sha256[:12],
            sidecar_note,
        )

    def _copy_and_maybe_patch(
        self, platform: str, source: Path, target: Path, mechanism: str
    ) -> None:
        """Copy the CI artifact into the dist dir, patching the slot if asked.

        Written through a temp file in the destination directory and renamed, so a
        reader can never observe a partially written binary and a crash cannot
        leave one behind (§5.3).
        """
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".sbs-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if mechanism == MECHANISM_PATCH:
                data = bytearray(source.read_bytes())
                original_size = len(data)
                count = patch_url_slot(data, self.public_url or "")
                if count == 0:
                    raise _NoSlotFound(platform)
                if len(data) != original_size:  # pragma: no cover - width guard
                    raise _NoSlotFound(platform)
                tmp.write_bytes(bytes(data))
            else:
                # `pristine` and `sidecar` both ship the CI bytes unchanged.
                shutil.copyfile(source, tmp)
            # 0755: this is an executable, and a download that loses the bit is
            # the exact problem the raw-format path exists to avoid.
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _rebuild(self, platform: str, target: Path) -> None:
        """Cross-compile with the URL baked in via -ldflags (§5.2 `rebuild`)."""
        go_bin = self._go_binary()
        if go_bin is None:
            raise _NoToolchain(platform)

        # `go build` runs from the module root; the binary's entry point is the
        # `cli/cmd/sbs` package inside it.
        go_root = self._go_module_root()
        if not (go_root / "go.mod").is_file():
            raise _NoToolchain(platform)

        url = validate_public_url(self.public_url or "", for_slot=True)
        goos, _, goarch = platform.partition("-")

        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".sbs-build-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            # `-X` addresses a variable by its package's full IMPORT PATH, not
            # by `main`. The injected variables live in the importable `cli`
            # package (client/go/cli/version.go) so that the test suite can live
            # in a separate directory — a `package main` cannot be imported.
            #
            # A stale `-X main.urlSlot=...` is accepted silently by the linker and
            # injects nothing, which is the same failure shape as the
            # constant-initializer gotcha of §3.4 #1: the build succeeds and the
            # artifact quietly points at its compile-time default. Hence
            # GO_LDFLAGS_PKG is a named constant, asserted against the Go source
            # by test_ldflags_target_matches_the_go_package.
            ldflags = (
                f"-s -w -X {GO_LDFLAGS_PKG}.URLSlot={url} "
                f"-X {GO_LDFLAGS_PKG}.Version={self._cli_version} "
                f"-X {GO_LDFLAGS_PKG}.EngineVersion={self._engine_version}"
            )
            env = {
                **os.environ,
                "CGO_ENABLED": "0",
                "GOOS": goos,
                "GOARCH": goarch,
                # A build triggered by a server start must never reach the
                # network for dependencies: it would make startup depend on
                # proxy.golang.org, and an air-gapped image would hang. Vendored
                # deps (`make cli-vendor`) or a warm module cache are required.
                "GOFLAGS": os.environ.get("GOFLAGS", ""),
            }
            # argv list, never a shell (§7.2, B14): `url` is validated above, but
            # exec-without-a-shell is what makes that validation a second line of
            # defence rather than the only one.
            proc = subprocess.run(
                [
                    go_bin,
                    "build",
                    "-trimpath",
                    "-ldflags",
                    ldflags,
                    "-o",
                    str(tmp),
                    GO_CMD_PKG,
                ],
                cwd=str(go_root),
                capture_output=True,
                text=True,
                env=env,
                timeout=600,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"go build for {platform} failed: {proc.stderr.strip()[:500]}"
                )
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _write_sidecar(self, platform: str) -> None:
        """Write `sbs.url` beside a sidecar-mechanism binary (§5.2).

        One line, no trailing structure: the CLI reads it on first run and folds
        it into its config. Simple on purpose — this file is parsed by a binary
        that may be the only thing the user has.
        """
        path = self.dist_dir / platform / "sbs.url"
        path.write_text(f"{self.public_url}\n", encoding="utf-8")

    def _build_archive(self, platform: str, entry: PlatformArtifact) -> None:
        """Build the browser-download archive (§5.5.2).

        `format=archive` is the browser default for three concrete reasons, not
        tidiness: a browser download loses the executable bit, macOS stamps it
        `com.apple.quarantine`, and the sidecar mechanism needs a second file to
        arrive alongside the first.

        Contents: the binary, `LICENSE.restish` (we redistribute an MIT binary),
        and `sbs.url` where the mechanism calls for it.

        # Why the archive is built deterministically

        Every field that would otherwise vary run to run — gzip's embedded mtime,
        each entry's mtime, uid/gid and uname/gname — is pinned. Without that the
        archive bytes differ on every preparation, and §5.3's guarantee that "two
        replicas that each prepared their own copy still agree" holds for the raw
        binary (patching is deterministic) but would not for the archive.

        That is not cosmetic. A client behind a load balancer can read the digest
        from replica A and fetch the archive from replica B, and both the CLI and
        any careful script *refuse* on a checksum mismatch — so non-reproducible
        archives surface as an intermittent, unreproducible "checksum mismatch"
        that looks exactly like an attack.
        """
        binary = self.artifact_path(platform, entry.filename)
        archive = self.archive_path(platform)
        license_path = self.artifacts_dir / "LICENSE.restish"
        sidecar = self.dist_dir / platform / "sbs.url"

        members = [(binary, entry.filename, 0o755)]
        for path, arcname in (
            (license_path, "LICENSE.restish"),
            (sidecar, "sbs.url"),
        ):
            if path.is_file():
                members.append((path, arcname, 0o644))

        fd, tmp_name = tempfile.mkstemp(dir=str(archive.parent), prefix=".sbs-arc-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if platform.startswith("windows-"):
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path, arcname, mode in members:
                        # A fixed date_time: ZipInfo.from_file would embed the
                        # staged file's mtime. 1980-01-01 is the earliest the ZIP
                        # format can represent.
                        info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                        # The high 16 bits carry the POSIX mode; 0o100000 marks a
                        # regular file. Without this, unzip on a POSIX host gives
                        # the binary no executable bit.
                        info.external_attr = (0o100000 | mode) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        zf.writestr(info, path.read_bytes())
            else:
                # gzip's own header carries an mtime, which tarfile.open("w:gz")
                # fills from the clock. Wrapping an explicit GzipFile(mtime=0) is
                # the only way to pin it.
                with open(tmp, "wb") as raw:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, mtime=0
                    ) as gz:
                        with tarfile.open(fileobj=gz, mode="w") as tf:
                            for path, arcname, mode in members:
                                info = tarfile.TarInfo(name=arcname)
                                info.size = path.stat().st_size
                                info.mode = mode
                                info.mtime = 0
                                info.type = tarfile.REGTYPE
                                # Numeric and name ownership both pinned: the
                                # server's own uid/gid are irrelevant to the user
                                # extracting this, and they vary per deployment.
                                info.uid = 0
                                info.gid = 0
                                info.uname = ""
                                info.gname = ""
                                with path.open("rb") as fh:
                                    tf.addfile(info, fh)
            os.chmod(tmp, 0o644)
            os.replace(tmp, archive)
        except BaseException:
            tmp.unlink(missing_ok=True)
            logger.exception("Building the %s download archive failed", platform)
            return

        entry.archive_filename = archive.name
        entry.archive_size = archive.stat().st_size
        entry.archive_sha256 = sha256_file(archive)


class _NoSlotFound(RuntimeError):
    """The prebuilt artifact contains no patchable URL slot."""


class _NoToolchain(RuntimeError):
    """`rebuild` was requested but no Go toolchain is present."""
