# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0
"""Which CLI artifact does this client want?

docs/design/new_cli.md §5.6. Four sources, in strict precedence order:

1. ``?platform=`` — always wins. It is what the UI sends, what a script pins and
   what a bug report reproduces.
2. **UA Client Hints** (``Sec-CH-UA-Platform`` / ``-Arch`` / ``-Bitness``).
   Chromium only, and the high-entropy hints arrive *only after* the origin has
   advertised them with ``Accept-CH`` — which is why the ``/ui`` handler sends
   that header.
3. **User-Agent** sniffing. Distinguishes Windows/macOS/Linux and, on Linux,
   x86-64 from aarch64.
4. Default ``linux-amd64``.

The sharp edge, and the reason the UI always shows a chooser: **Apple Silicon is
undecidable from a User-Agent.** Every Mac browser reports ``Intel Mac OS X
10_15_7`` regardless of the actual CPU — Safari and Firefox included, and they
send no Client Hints at all. So a Mac without hints gets ``darwin-arm64`` (the
overwhelmingly more likely machine to be downloading a CLI today) plus a header
saying the answer was guessed, rather than a confident wrong answer.

Detection is only ever a *default*. The CLI itself never relies on it: it sends
an explicit ``?platform=`` built from ``runtime.GOOS``/``runtime.GOARCH``, which
is exact. So is ``uname -sm`` in the install script.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

# The closed platform enum of §5.1. Server-side this is a *closed* set for a
# security reason as much as a correctness one (B13, §7.2): the value is resolved
# through a dict of prepared artifacts and never joined onto a filesystem path,
# so `platform=../../etc/passwd` cannot become a traversal. Adding a platform
# means adding it here AND building an artifact for it.
SUPPORTED_PLATFORMS: tuple[str, ...] = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)

DEFAULT_PLATFORM = "linux-amd64"

# The client hints we need in order to answer confidently. Requested from the UI
# with Accept-CH; the browser sends them on subsequent requests.
CLIENT_HINT_HEADERS = (
    "Sec-CH-UA-Platform",
    "Sec-CH-UA-Arch",
    "Sec-CH-UA-Bitness",
)

# Without Vary, one shared cache hands a Windows user the Linux binary (B11).
# User-Agent is included because it is a detection input here, not merely a
# logging field.
VARY_HEADER = ", ".join((*CLIENT_HINT_HEADERS, "User-Agent"))

# How the detection was reached, surfaced as X-SBS-Platform-Detection so a wrong
# guess is diagnosable from a single response rather than from a bug report.
SOURCE_PARAM = "param"
SOURCE_CLIENT_HINTS = "client-hints"
SOURCE_USER_AGENT = "user-agent"
SOURCE_GUESSED = "guessed"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class Detection:
    """A resolved platform plus how we got there."""

    platform: str
    source: str

    @property
    def is_confident(self) -> bool:
        """False when the client should be offered a chooser.

        ``guessed`` and ``default`` both mean "we picked something plausible and
        could be wrong" — the UI preselects but never hides the alternatives.
        """
        return self.source in (SOURCE_PARAM, SOURCE_CLIENT_HINTS, SOURCE_USER_AGENT)


def _norm(value: Optional[str]) -> str:
    """Lowercase, strip, and drop the quotes Client Hints arrive wrapped in.

    Client Hint values are sent as RFC 8941 structured-field strings, so
    ``Sec-CH-UA-Platform`` is literally ``"macOS"`` — quotes included. Comparing
    without stripping them silently never matches.
    """
    if not value:
        return ""
    return value.strip().strip('"').strip().lower()


def _get(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header read.

    Starlette's Headers is already case-insensitive, but this module is also
    called with plain dicts from tests and from the install-script generator.
    """
    if hasattr(headers, "get"):
        direct = headers.get(name)
        if direct:
            return direct
        # Plain dict with different casing.
        lowered = name.lower()
        for key, value in dict(headers).items():
            if key.lower() == lowered:
                return value
    return ""


def _from_client_hints(headers: Mapping[str, str]) -> Optional[Detection]:
    """Resolve from Sec-CH-UA-Platform/-Arch/-Bitness, or None if incomplete."""
    platform_hint = _norm(_get(headers, "Sec-CH-UA-Platform"))
    arch_hint = _norm(_get(headers, "Sec-CH-UA-Arch"))
    bitness_hint = _norm(_get(headers, "Sec-CH-UA-Bitness"))

    if not platform_hint:
        return None

    if platform_hint in ("macos", "mac os x"):
        goos = "darwin"
    elif platform_hint == "windows":
        goos = "windows"
    elif platform_hint in ("linux", "chrome os", "chromium os"):
        goos = "linux"
    else:
        # Android, iOS, unknown: we ship no artifact, so let the caller fall
        # through rather than inventing a platform id that cannot be served.
        return None

    # Arch is the reason to want hints at all — it is the one fact a User-Agent
    # cannot supply on macOS. Without it, defer.
    if not arch_hint:
        return None

    if arch_hint == "arm":
        # 32-bit ARM is not a platform we build for; only arm64 is.
        if bitness_hint and bitness_hint != "64":
            return None
        goarch = "arm64"
    elif arch_hint in ("x86", "x86_64", "amd64"):
        # `x86` with 64-bit bitness is how Chromium reports x86-64.
        if bitness_hint == "32":
            return None
        goarch = "amd64"
    else:
        return None

    candidate = f"{goos}-{goarch}"
    if candidate not in SUPPORTED_PLATFORMS:
        return None
    return Detection(candidate, SOURCE_CLIENT_HINTS)


# Ordered most-specific first: "Android" contains neither, but a Windows UA can
# also mention "Linux" in some embedded browsers, so Windows is tested first.
_UA_WINDOWS = re.compile(r"windows", re.IGNORECASE)
_UA_MAC = re.compile(r"mac os x|macintosh", re.IGNORECASE)
_UA_LINUX = re.compile(r"linux|x11", re.IGNORECASE)
_UA_ARM64 = re.compile(r"aarch64|arm64", re.IGNORECASE)


def _from_user_agent(user_agent: str) -> Optional[Detection]:
    """Resolve from the User-Agent, marking a macOS answer as guessed."""
    if not user_agent:
        return None

    if _UA_WINDOWS.search(user_agent):
        # We build only windows-amd64 today. A Windows-on-ARM user gets the
        # amd64 binary, which runs under emulation — so this is a working answer,
        # not a wrong one.
        return Detection("windows-amd64", SOURCE_USER_AGENT)

    if _UA_MAC.search(user_agent):
        # THE undecidable case. Every Mac browser reports "Intel Mac OS X
        # 10_15_7" — freezing that string was a deliberate anti-fingerprinting
        # move by Apple, so no amount of parsing will recover the real CPU.
        # Apple Silicon is the overwhelmingly more likely machine, so prefer it
        # and label the answer a guess.
        return Detection("darwin-arm64", SOURCE_GUESSED)

    if _UA_LINUX.search(user_agent):
        if _UA_ARM64.search(user_agent):
            return Detection("linux-arm64", SOURCE_USER_AGENT)
        return Detection("linux-amd64", SOURCE_USER_AGENT)

    return None


def detect_platform(
    headers: Mapping[str, str],
    explicit: Optional[str] = None,
) -> Detection:
    """Resolve the artifact platform for a request.

    ``explicit`` is the ``?platform=`` query value. An explicit value that is not
    in ``SUPPORTED_PLATFORMS`` is NOT silently replaced with a default — callers
    must reject it with a 400, because quietly serving a Linux binary to someone
    who asked for ``darwin-arm64`` is worse than an error. Use
    ``is_supported_platform`` to check before calling, or handle the returned
    ``platform`` being the requested-but-unknown value.
    """
    if explicit:
        # Returned as-is, valid or not; validation is the caller's 400 to raise.
        return Detection(explicit, SOURCE_PARAM)

    from_hints = _from_client_hints(headers)
    if from_hints is not None:
        return from_hints

    from_ua = _from_user_agent(_get(headers, "User-Agent"))
    if from_ua is not None:
        return from_ua

    return Detection(DEFAULT_PLATFORM, SOURCE_DEFAULT)


def is_supported_platform(platform: Optional[str]) -> bool:
    """Whether this is a platform id we recognise (a closed enum — B13)."""
    return platform in SUPPORTED_PLATFORMS


def accept_ch_headers() -> dict[str, str]:
    """Headers that ask a Chromium browser for the high-entropy hints.

    ``Accept-CH`` advertises what we want; ``Critical-CH`` tells the browser to
    *retry the current navigation* with them attached rather than waiting for the
    next request. Without Critical-CH the first visit to the UI would render with
    no arch information and the modal would preselect a guess — the second visit
    would be right, which is a confusing difference to debug.
    """
    return {
        "Accept-CH": ", ".join(CLIENT_HINT_HEADERS),
        "Critical-CH": ", ".join(CLIENT_HINT_HEADERS),
        # Permissions-Policy is what lets the hints be sent to this origin's
        # subresources too; without it some Chromium versions withhold them.
        "Permissions-Policy": ", ".join(
            f'ch-ua-{suffix}=("self")' for suffix in ("platform", "arch", "bitness")
        ),
    }
