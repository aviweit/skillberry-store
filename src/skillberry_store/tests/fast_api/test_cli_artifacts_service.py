# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Unit tests for the CLI artifact service — stamping, patching, manifests.

docs/design/new_cli.md §8.2 #7–#9 and #16. These are the pieces that decide what
bytes a user ends up executing, so the tests are about exactness rather than
shape: a patched slot that changes the file size, a sha256 recorded before
patching instead of after, or a stamp that ignores the public URL would each
produce a binary that runs and talks to the wrong store.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

from skillberry_store.services.cli_artifacts import (
    DIST_DIR,
    ARTIFACTS_DIR,
    MECHANISM_PATCH,
    MECHANISM_PRISTINE,
    MECHANISM_REBUILD,
    MECHANISM_SIDECAR,
    REASON_NOT_BUNDLED,
    REASON_NO_SLOT,
    SLOT_PAD,
    SLOT_PATTERN,
    SLOT_WIDTH,
    STATE_PREPARING,
    STATE_READY,
    STATE_UNAVAILABLE,
    CliArtifactService,
    CliArtifactSettings,
    UnacceptableURL,
    patch_url_slot,
    validate_public_url,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

ALL_PLATFORMS = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)


# --------------------------------------------------------------------------- #
# The Go/Python slot contract
# --------------------------------------------------------------------------- #


def test_slot_constants_match_the_go_source():
    """The single most dangerous drift in this feature.

    ``SLOT_WIDTH``/``SLOT_PATTERN`` here and ``slotWidth``/``urlSlot`` in
    client/go/cli/version.go are a contract between a Python writer and a Go reader. If
    they disagree, patching either finds nothing (caught — the platform reports
    ``no_slot_found``) or, far worse, writes a different number of bytes than the
    Go side trims, producing a binary that starts fine and points at a mangled
    URL. Nothing downstream would notice.
    """
    go_source = (REPO_ROOT / "client" / "go" / "cli" / "version.go").read_text(encoding="utf-8")

    width_match = re.search(r"const SlotWidth = (\d+)", go_source)
    assert width_match, "could not find `const SlotWidth` in client/go/cli/version.go"
    assert int(width_match.group(1)) == SLOT_WIDTH, (
        f"Go SlotWidth is {width_match.group(1)} but Python SLOT_WIDTH is "
        f"{SLOT_WIDTH}. A patched binary would be misread."
    )

    slot_match = re.search(r'var URLSlot = "([^"]*)"', go_source)
    assert slot_match, "could not find `var URLSlot` in client/go/cli/version.go"
    go_slot = slot_match.group(1).encode()
    assert go_slot == SLOT_PATTERN, (
        f"Go URLSlot is {go_slot!r} but Python SLOT_PATTERN is {SLOT_PATTERN!r}. "
        f"Patching would not find the slot."
    )
    assert len(go_slot) == SLOT_WIDTH, (
        f"Go URLSlot is {len(go_slot)} bytes, not SlotWidth ({SLOT_WIDTH})"
    )

    pad_match = re.search(r"const SlotPad = '(.)'", go_source)
    assert pad_match, "could not find `const SlotPad` in client/go/cli/version.go"
    assert pad_match.group(1).encode() == SLOT_PAD


def test_go_url_slot_has_a_constant_initializer():
    """§3.4 #1 / G8: `-ldflags -X` silently no-ops on a non-constant initializer.

    A prototype used ``"…" + strings.Repeat("#", 0)`` and the flag was ignored —
    the binary kept its source default and nothing failed. Guarded statically here
    as well as dynamically by the e2e dead-port control, because this is the shape
    that makes the whole `rebuild` mechanism a silent no-op.
    """
    go_source = (REPO_ROOT / "client" / "go" / "cli" / "version.go").read_text(encoding="utf-8")
    line = next(
        (ln for ln in go_source.splitlines() if ln.strip().startswith("var URLSlot")),
        None,
    )
    assert line, "client/go/cli/version.go declares no `var URLSlot`"
    assert re.fullmatch(r'var URLSlot = "[^"]*"', line.strip()), (
        f"URLSlot must be a plain string literal for -ldflags -X to work, got:\n"
        f"  {line.strip()}\n"
        f"An expression here makes -X a silent no-op (docs/design/new_cli.md §3.4 #1)."
    )


# --------------------------------------------------------------------------- #
# URL validation (§5.7, §7.2, B14) — §8.2 #16
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        'http://evil.com/"$(id)"',
        "http://evil.com/`id`",
        "http://evil.com/;id",
        "http://evil.com/$(id)",
        "http://evil.com/|whoami",
        "http://evil.com/ id",
        "http://evil.com/\nid",
        "http://evil.com/'",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com",
        "",
        "   ",
    ],
)
def test_validate_public_url_rejects_injection(bad):
    """The same validator gates a shell script, an -ldflags value and a patch."""
    with pytest.raises(UnacceptableURL):
        validate_public_url(bad)


@pytest.mark.parametrize(
    "good,expected",
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://store.example.com/", "https://store.example.com"),
        ("https://store.example.com/prefix/", "https://store.example.com/prefix"),
        ("  http://127.0.0.1:8099  ", "http://127.0.0.1:8099"),
        ("https://my-store.internal:8443/a/b", "https://my-store.internal:8443/a/b"),
    ],
)
def test_validate_public_url_normalises(good, expected):
    assert validate_public_url(good) == expected


def test_validate_public_url_enforces_slot_width_only_for_slots():
    long_url = "https://" + "a" * 80 + ".example.com"
    # Fine as a URL...
    assert validate_public_url(long_url) == long_url
    # ...but it cannot be written into a fixed-width slot.
    with pytest.raises(UnacceptableURL, match="exceeds"):
        validate_public_url(long_url, for_slot=True)


# --------------------------------------------------------------------------- #
# Patching — §8.2 #9
# --------------------------------------------------------------------------- #


def _fake_binary(slots: int = 3, filler: bytes = b"\x00ELF-ish padding\x00") -> bytearray:
    """A byte blob with `slots` copies of the URL slot, like a linked binary."""
    data = bytearray()
    for _ in range(slots):
        data += filler + SLOT_PATTERN + filler
    return data


def _padded(url: str) -> bytes:
    """The exact bytes a slot must hold for ``url``."""
    encoded = url.encode()
    return encoded + SLOT_PAD * (SLOT_WIDTH - len(encoded))


def _slot_offsets(data: bytes) -> list[int]:
    """Where the unpatched slots are in ``data``."""
    offsets, start = [], 0
    while (i := data.find(SLOT_PATTERN, start)) >= 0:
        offsets.append(i)
        start = i + SLOT_WIDTH
    return offsets


def _slots_after(before: bytes, after: bytes) -> list[bytes]:
    """Read each slot out of ``after``, at the offsets it occupied in ``before``.

    Reads the slots positionally and compares them for equality, rather than
    searching the whole blob for a URL substring. A URL that merely appears
    somewhere in a binary proves nothing about what the CLI will read at runtime:
    the value has to be in the slot, at the slot's offset, padded to exactly the
    slot's width. Patching is same-size and in place, so the offsets line up.
    """
    return [after[o : o + SLOT_WIDTH] for o in _slot_offsets(before)]


def test_patch_rewrites_every_slot_without_changing_size():
    data = _fake_binary(slots=3)
    pristine = bytes(data)
    original_size = len(data)

    url = "http://store.example.com:9443"
    count = patch_url_slot(data, url)

    assert count == 3, f"patched {count} slots, want 3"
    # The invariant that makes patching possible at all: a linked binary has
    # offsets (and on some formats checksums) that a length change invalidates.
    assert len(data) == original_size, "patching changed the file size"
    # Every slot holds exactly the padded URL, in place.
    assert _slots_after(pristine, bytes(data)) == [_padded(url)] * 3
    assert SLOT_PATTERN not in bytes(data), "an unpatched slot remains"


def test_patch_pads_to_exactly_slot_width():
    data = _fake_binary(slots=1)
    patch_url_slot(data, "http://s.io")

    offset = bytes(data).find(b"http://s.io")
    written = bytes(data)[offset : offset + SLOT_WIDTH]
    assert len(written) == SLOT_WIDTH
    # The Go side trims the padding, so it must be padding all the way to the
    # slot boundary — a short write would leave stale bytes of the old URL and
    # produce something like "http://s.ioalhost:8000".
    assert written == b"http://s.io" + SLOT_PAD * (SLOT_WIDTH - len(b"http://s.io"))


def test_patch_returns_zero_when_no_slot_present():
    """A CI artifact with no slot means a Go/Python version mismatch."""
    data = bytearray(b"no slot in here at all")
    assert patch_url_slot(data, "http://store.test") == 0


def test_patch_refuses_an_over_long_url_before_writing():
    """§8.2 #9: rejected *before* writing, so the buffer is untouched."""
    data = _fake_binary(slots=1)
    snapshot = bytes(data)
    with pytest.raises(UnacceptableURL, match="exceeds"):
        patch_url_slot(data, "http://" + "a" * 80 + ".com")
    assert bytes(data) == snapshot, "a rejected URL still modified the buffer"


def test_patch_refuses_an_injection_url():
    data = _fake_binary(slots=1)
    with pytest.raises(UnacceptableURL):
        patch_url_slot(data, 'http://evil.com/"$(id)"')


# --------------------------------------------------------------------------- #
# Fixtures for the service
# --------------------------------------------------------------------------- #


@pytest.fixture
def prebuilt_dir(tmp_path) -> Path:
    """A directory shaped like client/go/build.sh's output, with real slots."""
    source = tmp_path / "prebuilt"
    for platform in ALL_PLATFORMS:
        target_dir = source / platform
        target_dir.mkdir(parents=True)
        filename = "sbs.exe" if platform.startswith("windows-") else "sbs"
        # Distinct filler per platform so a test can prove it got the right one.
        (target_dir / filename).write_bytes(
            bytes(_fake_binary(slots=2, filler=platform.encode().ljust(24, b"\x00")))
        )
    (source / "LICENSE.restish").write_text("MIT License\n", encoding="utf-8")
    (source / "prebuilt-manifest.json").write_text(
        json.dumps(
            {
                "cli_name": "sbs",
                "cli_version": "1.2.3+gabc123",
                "engine": {"name": "restish", "version": "2.3.0", "license": "MIT"},
                "slot_width": SLOT_WIDTH,
            }
        ),
        encoding="utf-8",
    )
    return source


def _service(
    tmp_path,
    prebuilt_dir,
    *,
    public_url="http://store.test:8000",
    enabled=True,
    toolchain=False,
    cli_commit="gabc123",
):
    """A service pointed at a temporary tree.

    ``toolchain=False`` by default, which is both the production default (no
    compiler in the runtime image) and what keeps these tests fast: with a
    toolchain present, ``darwin-arm64`` is prepared by a real cross-compile, and
    tens of seconds per fixture would make the suite unusable. The rebuild path
    itself is covered by the mechanism-choice tests, which assert on the decision
    rather than running a compiler.
    """
    service = CliArtifactService(
        CliArtifactSettings(enabled=enabled),
        public_url=public_url,
        cli_commit=cli_commit,
        dist_dir=tmp_path / "dist",
        artifacts_dir=prebuilt_dir,
    )
    if not toolchain:
        service._go_binary = lambda: None  # type: ignore[method-assign]
    return service


# --------------------------------------------------------------------------- #
# Preparation and manifest shape — §8.2 #7
# --------------------------------------------------------------------------- #


def test_prepare_makes_every_platform_ready(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    manifest = svc.manifest()
    assert manifest["cli_name"] == "sbs"
    assert manifest["cli_version"] == "1.2.3+gabc123"
    assert manifest["public_url"] == "http://store.test:8000"
    assert manifest["engine"] == {
        "name": "restish",
        "version": "2.3.0",
        "license": "MIT",
    }

    for platform in ALL_PLATFORMS:
        entry = manifest["platforms"][platform]
        assert entry["state"] == STATE_READY, f"{platform}: {entry}"
        assert entry["sha256"], f"{platform} has no sha256"
        assert entry["size"] > 0
        # The record describes the artifact, not how to reach it: clients learn
        # an artifact's identity from the download response's headers.
        assert "download_url" not in entry
        assert "archive_url" not in entry


def test_windows_artifact_keeps_its_exe_extension(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    assert svc.manifest()["platforms"]["windows-amd64"]["filename"] == "sbs.exe"
    assert svc.manifest()["platforms"]["linux-amd64"]["filename"] == "sbs"


def test_sha256_is_computed_after_patching(tmp_path, prebuilt_dir):
    """§7.2: the manifest must describe the bytes served, not the CI bytes.

    Hashing before patching would publish a digest that every download fails to
    match — the install script and `sbs download-cli` both verify, so the feature
    would be comprehensively broken in a way only an end-to-end run would show.
    """
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    entry = svc.resolve("linux-amd64")
    served = svc.artifact_path("linux-amd64", entry.filename)
    on_disk = hashlib.sha256(served.read_bytes()).hexdigest()
    assert entry.sha256 == on_disk

    ci_bytes = (prebuilt_dir / "linux-amd64" / "sbs").read_bytes()
    assert entry.sha256 != hashlib.sha256(ci_bytes).hexdigest(), (
        "the recorded sha256 is the pre-patch one"
    )


def test_prepared_artifact_carries_the_public_url(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    source = (prebuilt_dir / "linux-amd64" / "sbs").read_bytes()
    prepared = svc.artifact_path("linux-amd64", "sbs").read_bytes()

    slots = _slots_after(source, prepared)
    assert slots, "the source artifact had no slot to patch"
    assert slots == [_padded("http://store.test:8000")] * len(slots)
    assert SLOT_PATTERN not in prepared, "an unpatched slot survived"


def test_darwin_arm64_uses_a_sidecar_without_a_toolchain(tmp_path, prebuilt_dir):
    """B3: Go ad-hoc signs darwin-arm64, so its bytes must not be patched.

    Patching inside the signed image invalidates the signature and macOS refuses
    to exec it — a failure the user sees as "killed", with no explanation.
    """
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    entry = svc.resolve("darwin-arm64")
    assert entry.url_injection == MECHANISM_SIDECAR
    body = svc.artifact_path("darwin-arm64", "sbs").read_bytes()
    assert SLOT_PATTERN in body, "darwin-arm64 was patched; its signature is now void"

    sidecar = svc.dist_dir / "darwin-arm64" / "sbs.url"
    assert sidecar.is_file(), "the sidecar mechanism wrote no sbs.url"
    assert sidecar.read_text(encoding="utf-8").strip() == "http://store.test:8000"

    # Other platforms still get a real patch.
    assert svc.resolve("linux-amd64").url_injection == MECHANISM_PATCH


def test_no_public_url_serves_pristine_artifacts(tmp_path, prebuilt_dir):
    """Truthful degradation: no URL to inject means the CI bytes, labelled."""
    svc = _service(tmp_path, prebuilt_dir, public_url=None)
    svc.prepare_all()

    entry = svc.resolve("linux-amd64")
    assert entry.state == STATE_READY
    assert entry.url_injection == MECHANISM_PRISTINE
    assert SLOT_PATTERN in svc.artifact_path("linux-amd64", "sbs").read_bytes()


def test_missing_artifacts_dir_reports_not_bundled(tmp_path):
    svc = _service(tmp_path, tmp_path / "does-not-exist")
    svc.prepare_all()

    for platform in ALL_PLATFORMS:
        entry = svc.manifest()["platforms"][platform]
        assert entry["state"] == STATE_UNAVAILABLE
        # A reason the UI and the CLI can show verbatim.
        assert entry["reason"] == REASON_NOT_BUNDLED


def test_artifact_without_a_slot_is_refused_not_served(tmp_path):
    """A slotless binary means a version mismatch; serving it misdirects users."""
    source = tmp_path / "prebuilt"
    (source / "linux-amd64").mkdir(parents=True)
    (source / "linux-amd64" / "sbs").write_bytes(b"a binary with no slot whatsoever")

    svc = _service(tmp_path, source)
    svc.prepare_all()

    entry = svc.manifest()["platforms"]["linux-amd64"]
    assert entry["state"] == STATE_UNAVAILABLE
    assert entry["reason"] == REASON_NO_SLOT


def test_flat_artifact_layout_is_accepted(tmp_path):
    """A release-asset download naturally yields `sbs-<platform>`, not a dir."""
    source = tmp_path / "prebuilt"
    source.mkdir()
    (source / "sbs-linux-amd64").write_bytes(bytes(_fake_binary(slots=1)))

    svc = _service(tmp_path, source)
    svc.prepare_all()
    assert svc.resolve("linux-amd64").state == STATE_READY


# --------------------------------------------------------------------------- #
# The stamp — §8.2 #8
# --------------------------------------------------------------------------- #


def test_unchanged_inputs_do_no_work(tmp_path, prebuilt_dir):
    """The property that makes a restart free."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    first = {
        platform: svc.artifact_path(platform, svc.resolve(platform).filename).stat()
        for platform in ALL_PLATFORMS
    }
    first_shas = {p: svc.resolve(p).sha256 for p in ALL_PLATFORMS}

    # A fresh service over the same dist dir, same URL, same commit.
    svc2 = _service(tmp_path, prebuilt_dir)
    svc2.load_manifest()
    svc2.prepare_all()

    for platform in ALL_PLATFORMS:
        entry = svc2.resolve(platform)
        assert entry.state == STATE_READY
        assert entry.sha256 == first_shas[platform]
        after = svc2.artifact_path(platform, entry.filename).stat()
        # mtime unchanged is the observable proof nothing was rewritten.
        assert after.st_mtime_ns == first[platform].st_mtime_ns, (
            f"{platform} was re-prepared despite unchanged inputs"
        )


def test_changed_public_url_re_prepares(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    old_sha = svc.resolve("linux-amd64").sha256

    svc2 = _service(tmp_path, prebuilt_dir, public_url="http://other.test:9000")
    svc2.load_manifest()
    svc2.prepare_all()

    entry = svc2.resolve("linux-amd64")
    assert entry.state == STATE_READY
    assert entry.sha256 != old_sha, "a changed public URL did not re-prepare"

    source = (prebuilt_dir / "linux-amd64" / "sbs").read_bytes()
    prepared = svc2.artifact_path("linux-amd64", entry.filename).read_bytes()
    slots = _slots_after(source, prepared)
    assert slots == [_padded("http://other.test:9000")] * len(slots), (
        "the slots do not hold the new URL"
    )


def test_changed_cli_commit_re_prepares(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    stamp_before = svc.resolve("linux-amd64").stamp

    # Same dist directory, different commit: the stamp is adopted from disk and
    # must then be invalidated by the commit change.
    svc2 = _service(tmp_path, prebuilt_dir, cli_commit="gdeadbee")
    svc2.load_manifest()
    assert svc2.resolve("linux-amd64").stamp == stamp_before  # adopted from disk
    svc2.prepare_all()
    assert svc2.resolve("linux-amd64").stamp != stamp_before, (
        "a new CLI commit must invalidate the stamp"
    )


def test_corrupt_artifact_re_prepares(tmp_path, prebuilt_dir):
    """The sha256 re-check is what catches a torn write or a truncated file."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    good_sha = svc.resolve("linux-amd64").sha256

    path = svc.artifact_path("linux-amd64", "sbs")
    path.write_bytes(b"corrupted")

    svc2 = _service(tmp_path, prebuilt_dir)
    svc2.load_manifest()
    svc2.prepare_all()

    assert svc2.resolve("linux-amd64").sha256 == good_sha
    assert svc2.artifact_path("linux-amd64", "sbs").read_bytes() != b"corrupted"


def test_missing_artifact_re_prepares(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    svc.artifact_path("linux-amd64", "sbs").unlink()

    svc2 = _service(tmp_path, prebuilt_dir)
    svc2.load_manifest()
    svc2.prepare_all()
    assert svc2.artifact_path("linux-amd64", "sbs").is_file()




def test_download_disabled_does_nothing(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir, enabled=False)
    svc.prepare_all()
    assert not svc.dist_dir.exists() or not any(svc.dist_dir.rglob("sbs"))


def test_stamp_depends_on_every_documented_input(tmp_path, prebuilt_dir):
    """§5.3: the key is over (url, commit, engine, platform, mechanism)."""
    svc = _service(tmp_path, prebuilt_dir)
    svc._engine_version = "2.3.0"
    base = svc.stamp_key("linux-amd64", MECHANISM_PATCH)

    assert svc.stamp_key("linux-arm64", MECHANISM_PATCH) != base, "platform ignored"
    assert svc.stamp_key("linux-amd64", MECHANISM_REBUILD) != base, "mechanism ignored"

    other_url = _service(tmp_path, prebuilt_dir, public_url="http://z.test")
    other_url._engine_version = "2.3.0"
    assert other_url.stamp_key("linux-amd64", MECHANISM_PATCH) != base, "url ignored"

    other_engine = _service(tmp_path, prebuilt_dir)
    other_engine._engine_version = "2.4.0"
    assert other_engine.stamp_key("linux-amd64", MECHANISM_PATCH) != base, (
        "engine version ignored"
    )


def test_manifest_for_a_different_public_url_is_not_adopted(tmp_path, prebuilt_dir):
    """Otherwise a changed URL would advertise stale artifacts as ready."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    svc2 = _service(tmp_path, prebuilt_dir, public_url="http://different.test")
    svc2.load_manifest()
    assert svc2.resolve("linux-amd64").state != STATE_READY, (
        "a manifest prepared for another URL was adopted; its binaries point "
        "somewhere else"
    )


# --------------------------------------------------------------------------- #
# Archives (§5.5.2)
# --------------------------------------------------------------------------- #


def test_tar_archive_contains_binary_licence_and_preserves_mode(
    tmp_path, prebuilt_dir
):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    archive = svc.archive_path("linux-amd64")
    assert archive.is_file()
    with tarfile.open(archive) as tf:
        names = tf.getnames()
        assert "sbs" in names
        # We redistribute an MIT binary, so its licence travels with it.
        assert "LICENSE.restish" in names
        # The executable bit is the whole reason `archive` is the browser default.
        assert tf.getmember("sbs").mode & 0o111, "the archived binary is not executable"

    entry = svc.resolve("linux-amd64")
    assert entry.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert entry.archive_size == archive.stat().st_size


def test_windows_archive_is_a_zip(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    archive = svc.archive_path("windows-amd64")
    assert archive.name.endswith(".zip")
    with zipfile.ZipFile(archive) as zf:
        assert "sbs.exe" in zf.namelist()
        assert "LICENSE.restish" in zf.namelist()


def test_sidecar_platform_archive_carries_the_url_file(tmp_path, prebuilt_dir):
    """The sidecar mechanism's binary has no baked URL, so the file must ship."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    with tarfile.open(svc.archive_path("darwin-arm64")) as tf:
        assert "sbs.url" in tf.getnames(), (
            "the darwin-arm64 archive omits sbs.url, so a download would point "
            "at the compile-time default"
        )


# --------------------------------------------------------------------------- #
# Manifest persistence and helpers
# --------------------------------------------------------------------------- #


def test_manifest_is_written_and_reloadable(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    path = svc.manifest_path()
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    # The stamp is persisted but must not be in the served document.
    assert "stamp" in doc["platforms"]["linux-amd64"]
    assert "stamp" not in svc.manifest()["platforms"]["linux-amd64"]

    # No temp files left behind.
    assert not list(svc.dist_dir.glob(".manifest-*"))


def test_load_manifest_tolerates_garbage(tmp_path, prebuilt_dir):
    """The dist dir is a cache; a broken manifest means "nothing prepared"."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.dist_dir.mkdir(parents=True)
    svc.manifest_path().write_text("{not json at all", encoding="utf-8")
    svc.load_manifest()  # must not raise
    assert svc.resolve("linux-amd64").state == STATE_UNAVAILABLE


def test_mark_preparing_only_touches_unready_platforms(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()
    svc._platforms["linux-arm64"].state = STATE_UNAVAILABLE

    svc.mark_preparing()
    # A ready platform stays downloadable while others are being prepared.
    assert svc.resolve("linux-amd64").state == STATE_READY
    assert svc.resolve("linux-arm64").state == STATE_PREPARING


def test_preparing_entry_advertises_retry_after(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir)
    svc.mark_preparing()
    entry = svc.manifest()["platforms"]["linux-amd64"]
    assert entry["state"] == STATE_PREPARING
    assert entry["retry_after"] == 10




# --------------------------------------------------------------------------- #
# Path safety — §8.2 #14 / B13
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "..",
        ".",
        "linux-amd64/../../../etc",
        "",
        "/etc/passwd",
        "linux-amd64;rm -rf /",
    ],
)
def test_artifact_path_rejects_anything_outside_the_enum(tmp_path, prebuilt_dir, hostile):
    """`platform` never reaches a path join without passing the closed enum."""
    svc = _service(tmp_path, prebuilt_dir)
    with pytest.raises(ValueError):
        svc.artifact_path(hostile)


def test_artifact_path_rejects_an_unsafe_filename(tmp_path, prebuilt_dir):
    """Belt and braces: filename comes from the manifest, which could be edited."""
    svc = _service(tmp_path, prebuilt_dir)
    for bad in ("../sbs", "a/b", "..", ""):
        if bad == "":
            continue  # empty falls back to the default name, which is safe
        with pytest.raises(ValueError):
            svc.artifact_path("linux-amd64", bad)


# --------------------------------------------------------------------------- #
# Configuration (§6)
# --------------------------------------------------------------------------- #
# One switch. Everything preparing and serving an artifact needs is derived: the
# directories are fixed beside the CLI source, the injection mechanism follows
# from the platform and from whether a toolchain is present, and preparation runs
# exactly when the feature is on.


def test_download_is_on_by_default(monkeypatch):
    monkeypatch.delenv("SBS_CLI_DOWNLOAD", raising=False)
    assert CliArtifactSettings.from_env().enabled is True


@pytest.mark.parametrize("value", ["off", "OFF", "false", "0", "no", ""])
def test_download_can_be_switched_off(monkeypatch, value):
    """The documented rollback for the whole feature (§9)."""
    monkeypatch.setenv("SBS_CLI_DOWNLOAD", value)
    assert CliArtifactSettings.from_env().enabled is False


@pytest.mark.parametrize("value", ["on", "true", "1", "yes", "anything-else"])
def test_download_stays_on_for_other_values(monkeypatch, value):
    monkeypatch.setenv("SBS_CLI_DOWNLOAD", value)
    assert CliArtifactSettings.from_env().enabled is True


def test_sbs_cli_download_is_the_only_environment_variable():
    """No other SBS_CLI_* variable is read anywhere.

    Directories, the injection mechanism, the concurrency cap and whether to
    prepare are all derived, so a deployment has exactly one knob. This asserts it
    against the source rather than by behaviour, because an unread variable is
    invisible from the outside — it would just sit in the code inviting someone to
    set it and wonder why nothing changed.
    """
    source = (
        REPO_ROOT / "src" / "skillberry_store" / "services" / "cli_artifacts.py"
    ).read_text(encoding="utf-8")
    api = (
        REPO_ROOT / "src" / "skillberry_store" / "fast_api" / "cli_api.py"
    ).read_text(encoding="utf-8")

    # Every occurrence, not just a quoted one. The narrower r'"(SBS_CLI_...)"'
    # this used to spell missed a bare mention inside a log message -- which is
    # exactly where a removed variable had survived, telling operators to "set
    # SBS_CLI_ARTIFACTS_DIR" when nothing reads it.
    found = set(re.findall(r"(SBS_CLI_[A-Z_]+)", source + api))
    assert found == {"SBS_CLI_DOWNLOAD"}, (
        f"unexpected SBS_CLI_* variables in the code: {sorted(found - {'SBS_CLI_DOWNLOAD'})}"
    )


def test_directories_are_fixed_at_the_top_of_the_tree():
    """Not configurable: the artifacts are a function of the source tree."""
    assert ARTIFACTS_DIR == REPO_ROOT / "cli-prebuilt"
    assert DIST_DIR == REPO_ROOT / "cli-dist"
    # Absolute, so nothing depends on the process's working directory.
    assert ARTIFACTS_DIR.is_absolute() and DIST_DIR.is_absolute()


def test_artifacts_dir_is_where_the_build_writes():
    """The read location and the write location are one fact, so pin them together.

    This is the assertion whose absence let the two drift: the service read
    client/go/cli/prebuilt while every producer wrote cli-prebuilt/, so a local
    `make cli-dist` left the server reporting `not_bundled` for all five
    platforms. Checked against the producers' own text -- a test that restated
    the path would drift with it.
    """
    build_sh = (REPO_ROOT / "client" / "go" / "build.sh").read_text(encoding="utf-8")
    assert f'OUT_DIR="$REPO_ROOT/{ARTIFACTS_DIR.name}"' in build_sh, (
        f"build.sh does not default --out to {ARTIFACTS_DIR.name}; the server "
        f"would read a directory the build never writes"
    )

    dev_mk = (REPO_ROOT / ".mk" / "dev.mk").read_text(encoding="utf-8")
    assert f"CLI_PREBUILT   := {ARTIFACTS_DIR.name}" in dev_mk, (
        f"CLI_PREBUILT in .mk/dev.mk no longer matches {ARTIFACTS_DIR.name}, so "
        f"`make cli-dist` writes where the server does not look"
    )

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"/app/{ARTIFACTS_DIR.name}" in dockerfile, (
        f"the runtime image no longer lands artifacts at /app/{ARTIFACTS_DIR.name}, "
        f"which is what ARTIFACTS_DIR resolves to when the app lives at /app"
    )


def test_a_service_uses_the_fixed_directories_by_default():
    svc = CliArtifactService(CliArtifactSettings())
    assert svc.artifacts_dir == ARTIFACTS_DIR
    assert svc.dist_dir == DIST_DIR


def test_concurrency_cap_is_fixed():
    assert CliArtifactSettings().max_concurrent_downloads == 8


# --------------------------------------------------------------------------- #
# Archive determinism (§5.3's replica-agreement guarantee)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("platform", ["linux-amd64", "windows-amd64"])
def test_archives_are_byte_identical_across_preparations(tmp_path, prebuilt_dir, platform):
    """Two replicas preparing the same inputs must produce the same archive.

    §5.3 promises the content-hash ETag makes independent copies agree. That holds
    for the raw binary because patching is deterministic, but an archive is not
    deterministic by default: gzip embeds an mtime and tar embeds each entry's
    mtime, uid/gid and uname/gname.

    The consequence is sharper than "hashes differ". A client can read the
    manifest from replica A and fetch the archive from replica B, and both the
    install script and `sbs download-cli` are written to REFUSE on a checksum
    mismatch — so the user sees an intermittent "checksum mismatch, refusing to
    install" that is indistinguishable from tampering — so every field that would
    otherwise vary run to run has to be pinned.
    """
    first = _service(tmp_path / "a", prebuilt_dir)
    first.prepare_all()
    first_bytes = first.archive_path(platform).read_bytes()

    second = _service(tmp_path / "b", prebuilt_dir)
    second.prepare_all()
    second_bytes = second.archive_path(platform).read_bytes()

    assert first_bytes == second_bytes, (
        f"the {platform} archive is not reproducible, so two replicas would "
        f"publish different archive_sha256 values for identical inputs"
    )
    assert (
        first.resolve(platform).archive_sha256
        == second.resolve(platform).archive_sha256
    )


def test_zip_archive_preserves_the_executable_bit(tmp_path, prebuilt_dir):
    """Pinning ZipInfo by hand must not lose the POSIX mode.

    `ZipInfo(...)` starts with external_attr = 0, so unzipping on a POSIX host
    would produce a non-executable binary — the very problem `format=archive`
    exists to avoid.
    """
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    with zipfile.ZipFile(svc.archive_path("windows-amd64")) as zf:
        info = zf.getinfo("sbs.exe")
        mode = (info.external_attr >> 16) & 0o777
        assert mode & 0o111, f"the archived sbs.exe has mode {mode:o}, not executable"


def test_tar_entries_carry_no_server_identity(tmp_path, prebuilt_dir):
    """The server's uid/gid/username are irrelevant to whoever extracts this."""
    svc = _service(tmp_path, prebuilt_dir)
    svc.prepare_all()

    with tarfile.open(svc.archive_path("linux-amd64")) as tf:
        for member in tf.getmembers():
            assert member.uid == 0 and member.gid == 0, member.name
            assert member.uname == "" and member.gname == "", member.name
            assert member.mtime == 0, member.name


# --------------------------------------------------------------------------- #
# The `-ldflags -X` package path
# --------------------------------------------------------------------------- #
#
# `-X` addresses a variable by its package's full import path. When the CLI moved
# from a single `package main` to an importable `cli` package plus a thin
# `cli/cmd/sbs` entry point (so the tests could live in client/go/tests), every
# `-X main.urlSlot=...` had to become
# `-X github.com/.../client/go/cli.URLSlot=...`.
#
# A wrong package path there is the worst kind of wrong: the linker accepts it
# **silently** and injects nothing, so the build succeeds and the artifact quietly
# keeps its compile-time default URL. That is the same failure shape as the
# constant-initializer gotcha of §3.4 #1 — and the reason the design demands a
# dead-port control test. These assertions catch the drift statically, in all
# four places that spell the path.


def test_ldflags_target_matches_the_go_package():
    """The Python `-X` target must name the package that declares the variables."""
    from skillberry_store.services.cli_artifacts import GO_CMD_PKG, GO_LDFLAGS_PKG

    go_mod = (REPO_ROOT / "client" / "go" / "go.mod").read_text(encoding="utf-8")
    module = re.search(r"^module (\S+)", go_mod, re.MULTILINE)
    assert module, "could not read the module path from client/go/go.mod"

    expected = f"{module.group(1)}/cli"
    assert GO_LDFLAGS_PKG == expected, (
        f"GO_LDFLAGS_PKG is {GO_LDFLAGS_PKG!r} but the `cli` package's import "
        f"path is {expected!r}. `-X` with a wrong package path is silently "
        f"ignored, so `rebuild` would produce artifacts with no URL baked in."
    )
    assert GO_LDFLAGS_PKG != "main", "the variables no longer live in package main"

    # And the variables really are declared there, under these exact names.
    version_go = (
        REPO_ROOT / "client" / "go" / "cli" / "version.go"
    ).read_text(encoding="utf-8")
    for name in ("URLSlot", "Version", "EngineVersion"):
        assert re.search(rf"^var {name} = ", version_go, re.MULTILINE), (
            f"`var {name}` is not declared in client/go/cli/version.go, so "
            f"-X {GO_LDFLAGS_PKG}.{name} would inject nothing"
        )

    # The build target is the `main` package, not the library — `go build` on a
    # library produces no binary at all.
    cmd_dir = REPO_ROOT / "client" / "go" / GO_CMD_PKG.removeprefix("./")
    main_go = cmd_dir / "main.go"
    assert main_go.is_file(), f"{GO_CMD_PKG} has no main.go"
    assert re.search(r"^package main$", main_go.read_text(encoding="utf-8"), re.MULTILINE), (
        f"{GO_CMD_PKG} is not a main package, so `go build` would emit no binary"
    )


def test_every_ldflags_caller_uses_the_same_package_path():
    """Four places spell the `-X` package path; all must agree.

    The build script, the makefile, this service and the CI workflow each invoke
    the linker independently. One left on `main.` would silently stop injecting,
    and only an end-to-end run against a live store would notice.
    """
    from skillberry_store.services.cli_artifacts import GO_LDFLAGS_PKG

    callers = {
        "client/go/build.sh": REPO_ROOT / "client" / "go" / "build.sh",
        ".mk/dev.mk": REPO_ROOT / ".mk" / "dev.mk",
        ".github/workflows/cli-artifacts.yml": (
            REPO_ROOT / ".github" / "workflows" / "cli-artifacts.yml"
        ),
    }
    for label, path in callers.items():
        if not path.is_file():
            continue
        # Comment lines are skipped deliberately: all three files *document* the
        # `-X main.…` pitfall in prose, and matching that text would make this
        # test fail on the very comments that exist to prevent the mistake.
        code = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().lstrip("@").startswith("#")
        ]
        text = "\n".join(code)
        if "ldflags" not in text:
            continue
        for stale in ("-X main.", "main.urlSlot", "main.version", "main.engineVersion"):
            assert stale not in text, (
                f"{label} still passes `{stale}…`, which the linker ignores "
                f"silently now that the variables live in {GO_LDFLAGS_PKG}"
            )
        # And it must actually name the right package somewhere.
        assert GO_LDFLAGS_PKG in text or "CLI_PKG" in text, (
            f"{label} invokes the linker but never names {GO_LDFLAGS_PKG}"
        )


# --------------------------------------------------------------------------- #
# Mechanism choice — the cheapest thing that works, per platform
# --------------------------------------------------------------------------- #


def test_patch_covers_every_platform_it_can(tmp_path, prebuilt_dir):
    """Four of five platforms never need a compiler, toolchain present or not.

    `patch` is milliseconds and produces a binary indistinguishable from a
    rebuilt one, so spending tens of seconds cross-compiling those four would buy
    nothing. Asserted with a toolchain available, because that is the case where
    a "rebuild if you can" rule would quietly make startup minutes long.
    """
    svc = _service(tmp_path, prebuilt_dir, toolchain=True)
    for platform in ("linux-amd64", "linux-arm64", "darwin-amd64", "windows-amd64"):
        assert svc._choose_mechanism(platform) == MECHANISM_PATCH, platform


def test_darwin_arm64_rebuilds_when_a_toolchain_exists(tmp_path, prebuilt_dir):
    """The one cell where a toolchain earns its keep (B3)."""
    svc = _service(tmp_path, prebuilt_dir, toolchain=True)
    assert svc._choose_mechanism("darwin-arm64") == MECHANISM_REBUILD


def test_darwin_arm64_falls_back_to_a_sidecar_without_one(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir, toolchain=False)
    assert svc._choose_mechanism("darwin-arm64") == MECHANISM_SIDECAR


def test_no_public_url_means_pristine(tmp_path, prebuilt_dir):
    svc = _service(tmp_path, prebuilt_dir, public_url=None, toolchain=True)
    for platform in ALL_PLATFORMS:
        assert svc._choose_mechanism(platform) == MECHANISM_PRISTINE, platform


# --------------------------------------------------------------------------- #
# Publication order
# --------------------------------------------------------------------------- #


def test_ready_is_published_only_once_every_variant_exists(tmp_path, prebuilt_dir):
    """`state = ready` must not be observable before the archive is on disk.

    The download route reads `state` to decide whether to serve. Setting it while
    the archive is still being written advertises `format=archive` for a file that
    does not exist yet — gzipping a ~33 MB binary is not instant, so a client
    asking inside that window gets a 503 for something the record already calls
    ready. Publishing last makes "ready" mean ready for every variant.

    Observed by watching the entry from inside archive construction, which is the
    only moment the ordering is distinguishable.
    """
    svc = _service(tmp_path, prebuilt_dir)

    seen: list[tuple[str, bool]] = []
    real_build = svc._build_archive

    def watching_build(platform, entry):
        # What a concurrent request would see at this instant.
        seen.append((entry.state, svc.archive_path(platform).is_file()))
        return real_build(platform, entry)

    svc._build_archive = watching_build  # type: ignore[method-assign]
    svc.prepare_all()

    assert seen, "the archive step never ran"
    for state, archive_exists in seen:
        assert state != STATE_READY, (
            "the entry was already 'ready' while its archive was still being "
            "built, so format=archive was advertised for a missing file"
        )
        assert not archive_exists

    # And once preparation is done, both variants really are present.
    for platform in ALL_PLATFORMS:
        entry = svc.resolve(platform)
        assert entry.state == STATE_READY, platform
        assert svc.artifact_path(platform, entry.filename).is_file(), platform
        assert svc.archive_path(platform).is_file(), platform
        assert entry.archive_filename and entry.archive_sha256, platform


def test_a_failed_archive_leaves_no_archive_record(tmp_path, prebuilt_dir):
    """So the route can tell "no archive" from "archive at a derived path".

    The handler locates the archive from the record; if building it fails the
    record must stay empty, or the route would advertise a path nothing wrote.
    """
    svc = _service(tmp_path, prebuilt_dir)
    svc._build_archive = lambda platform, entry: None  # type: ignore[method-assign]
    svc.prepare_all()

    entry = svc.resolve("linux-amd64")
    # The raw artifact is still usable — that is the primary download.
    assert entry.state == STATE_READY
    assert svc.artifact_path("linux-amd64", entry.filename).is_file()
    # But nothing claims an archive.
    assert entry.archive_filename == ""
    assert entry.archive_sha256 == ""
