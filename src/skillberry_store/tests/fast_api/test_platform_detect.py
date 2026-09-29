# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Platform detection for CLI downloads — docs/design/new_cli.md §5.6, §8.2 #11.

The detection matrix, and the one case that has no right answer: **Apple Silicon
is undecidable from a User-Agent.** Every Mac browser reports ``Intel Mac OS X
10_15_7`` — Apple froze that string deliberately — and Safari and Firefox send no
Client Hints at all. So a Mac without hints must be *labelled a guess*, not
answered confidently, because the UI uses that label to decide whether to
insist on a chooser.
"""

from __future__ import annotations

import pytest

from skillberry_store.fast_api.platform_detect import (
    CLIENT_HINT_HEADERS,
    DEFAULT_PLATFORM,
    SOURCE_CLIENT_HINTS,
    SOURCE_DEFAULT,
    SOURCE_GUESSED,
    SOURCE_PARAM,
    SOURCE_USER_AGENT,
    SUPPORTED_PLATFORMS,
    VARY_HEADER,
    accept_ch_headers,
    detect_platform,
    is_supported_platform,
)

# Real User-Agent strings, because that is what the parsing has to survive.
UA_MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
UA_MAC_FIREFOX = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0"
)
UA_WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
UA_LINUX_CHROME = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
UA_LINUX_ARM = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
UA_CURL = "curl/8.5.0"


# --------------------------------------------------------------------------- #
# 1. The param always wins
# --------------------------------------------------------------------------- #


def test_explicit_param_beats_everything():
    """What the UI sends, what a script pins, what a bug report reproduces."""
    got = detect_platform(
        {"User-Agent": UA_WINDOWS_CHROME, "Sec-CH-UA-Platform": '"Windows"'},
        explicit="darwin-arm64",
    )
    assert got.platform == "darwin-arm64"
    assert got.source == SOURCE_PARAM
    assert got.is_confident


def test_unknown_explicit_param_is_returned_verbatim():
    """So the caller can 400 rather than silently substitute a default.

    Handing someone a Linux binary because they asked for something unrecognised
    is worse than an error — they would run it and get "cannot execute binary
    file", with nothing pointing at the cause.
    """
    got = detect_platform({}, explicit="plan9-mips")
    assert got.platform == "plan9-mips"
    assert not is_supported_platform(got.platform)


# --------------------------------------------------------------------------- #
# 2. Client Hints
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "platform_hint,arch_hint,bitness,expected",
    [
        ('"macOS"', '"arm"', '"64"', "darwin-arm64"),
        ('"macOS"', '"x86"', '"64"', "darwin-amd64"),
        ('"Windows"', '"x86"', '"64"', "windows-amd64"),
        ('"Linux"', '"x86"', '"64"', "linux-amd64"),
        ('"Linux"', '"arm"', '"64"', "linux-arm64"),
        # Chromium on ChromeOS reports "Chrome OS"; a Linux artifact is right.
        ('"Chrome OS"', '"x86"', '"64"', "linux-amd64"),
        # Values arrive quoted as RFC 8941 structured strings; unquoted must work
        # too, since not every client is strict.
        ("macOS", "arm", "64", "darwin-arm64"),
    ],
)
def test_client_hints_resolve_exactly(platform_hint, arch_hint, bitness, expected):
    got = detect_platform(
        {
            "Sec-CH-UA-Platform": platform_hint,
            "Sec-CH-UA-Arch": arch_hint,
            "Sec-CH-UA-Bitness": bitness,
            # A macOS UA is present and says "Intel"; the hints must win, or
            # Apple Silicon users get the Intel binary.
            "User-Agent": UA_MAC_SAFARI,
        }
    )
    assert got.platform == expected
    assert got.source == SOURCE_CLIENT_HINTS
    assert got.is_confident


def test_client_hints_beat_a_contradicting_user_agent():
    """The entire point of asking for hints."""
    got = detect_platform(
        {
            "Sec-CH-UA-Platform": '"macOS"',
            "Sec-CH-UA-Arch": '"arm"',
            "Sec-CH-UA-Bitness": '"64"',
            "User-Agent": UA_MAC_SAFARI,  # claims Intel
        }
    )
    assert got.platform == "darwin-arm64"


def test_platform_hint_without_arch_falls_through():
    """A platform hint alone adds nothing over the User-Agent.

    Arch is the one fact a UA cannot supply on macOS, so an incomplete hint set
    must not be treated as confident — otherwise a Mac would resolve to
    darwin-amd64 by default and Apple Silicon users would silently get Rosetta.
    """
    got = detect_platform(
        {"Sec-CH-UA-Platform": '"macOS"', "User-Agent": UA_MAC_SAFARI}
    )
    assert got.source == SOURCE_GUESSED
    assert got.platform == "darwin-arm64"


def test_32_bit_is_not_served():
    """We build no 32-bit artifact; guessing a 64-bit one would not run."""
    got = detect_platform(
        {
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Arch": '"x86"',
            "Sec-CH-UA-Bitness": '"32"',
            "User-Agent": UA_WINDOWS_CHROME,
        }
    )
    # Falls through to the UA, which yields the amd64 build — a working answer on
    # 64-bit Windows and the only one we have.
    assert got.source == SOURCE_USER_AGENT


def test_mobile_platform_hints_fall_through():
    """Android/iOS get no artifact; inventing an id we cannot serve is worse."""
    for hint in ('"Android"', '"iOS"', '"Unknown"'):
        got = detect_platform({"Sec-CH-UA-Platform": hint, "Sec-CH-UA-Arch": '"arm"'})
        assert got.source in (SOURCE_DEFAULT, SOURCE_USER_AGENT)


# --------------------------------------------------------------------------- #
# 3. User-Agent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ua,expected,source",
    [
        (UA_WINDOWS_CHROME, "windows-amd64", SOURCE_USER_AGENT),
        (UA_LINUX_CHROME, "linux-amd64", SOURCE_USER_AGENT),
        (UA_LINUX_ARM, "linux-arm64", SOURCE_USER_AGENT),
        # The undecidable case: prefer Apple Silicon, label it a guess.
        (UA_MAC_SAFARI, "darwin-arm64", SOURCE_GUESSED),
        (UA_MAC_FIREFOX, "darwin-arm64", SOURCE_GUESSED),
    ],
)
def test_user_agent_matrix(ua, expected, source):
    got = detect_platform({"User-Agent": ua})
    assert got.platform == expected
    assert got.source == source


def test_a_mac_answer_is_never_confident():
    """The property the UI depends on to insist on a chooser.

    If this flipped to confident, an Intel Mac user would be handed an arm64
    binary with no visible alternative and the failure would look like a corrupt
    download.
    """
    for ua in (UA_MAC_SAFARI, UA_MAC_FIREFOX):
        assert not detect_platform({"User-Agent": ua}).is_confident


def test_windows_is_confident_despite_only_one_arch():
    """Windows-on-ARM runs the amd64 binary under emulation, so this is correct."""
    assert detect_platform({"User-Agent": UA_WINDOWS_CHROME}).is_confident


# --------------------------------------------------------------------------- #
# 4. Default
# --------------------------------------------------------------------------- #


def test_no_signals_at_all_defaults_and_says_so():
    got = detect_platform({})
    assert got.platform == DEFAULT_PLATFORM
    assert got.source == SOURCE_DEFAULT
    # Marked as a guess so a wrong answer is diagnosable from one response.
    assert not got.is_confident


def test_curl_gets_the_default():
    """`curl /cli/download` with no param: a default plus a diagnostic header.

    The install script sends `uname -sm`-derived ?platform=, so this only affects
    someone hand-rolling a request.
    """
    got = detect_platform({"User-Agent": UA_CURL})
    assert got.platform == DEFAULT_PLATFORM
    assert not got.is_confident


def test_case_insensitive_header_lookup():
    """Plain dicts with arbitrary casing must work, not just Starlette Headers."""
    got = detect_platform({"user-agent": UA_WINDOWS_CHROME})
    assert got.platform == "windows-amd64"
    got = detect_platform(
        {
            "sec-ch-ua-platform": '"macOS"',
            "SEC-CH-UA-ARCH": '"arm"',
            "Sec-Ch-Ua-Bitness": '"64"',
        }
    )
    assert got.platform == "darwin-arm64"


# --------------------------------------------------------------------------- #
# The enum, Vary and Accept-CH
# --------------------------------------------------------------------------- #


def test_supported_platforms_is_the_documented_closed_set():
    assert set(SUPPORTED_PLATFORMS) == {
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
    }
    assert DEFAULT_PLATFORM in SUPPORTED_PLATFORMS
    for platform in SUPPORTED_PLATFORMS:
        goos, _, goarch = platform.partition("-")
        assert goos and goarch, f"{platform} is not <goos>-<goarch>"


@pytest.mark.parametrize(
    "value", ["", None, "linux", "amd64", "../../etc/passwd", "LINUX-AMD64"]
)
def test_is_supported_platform_is_strict(value):
    """A closed enum is what stops `platform` ever reaching a path join (B13)."""
    assert not is_supported_platform(value)


def test_vary_covers_every_detection_input():
    """Without this, one shared cache serves a Windows user the Linux binary."""
    for header in CLIENT_HINT_HEADERS:
        assert header in VARY_HEADER
    assert "User-Agent" in VARY_HEADER


def test_accept_ch_advertises_the_high_entropy_hints():
    """B10: Arch and Bitness are withheld until the origin asks for them."""
    headers = accept_ch_headers()
    for header in CLIENT_HINT_HEADERS:
        assert header in headers["Accept-CH"]
    # Critical-CH makes the browser retry the *current* navigation with the
    # hints, so the first page load is already accurate.
    for header in CLIENT_HINT_HEADERS:
        assert header in headers["Critical-CH"]
