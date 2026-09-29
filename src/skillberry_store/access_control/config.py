"""Access-control YAML config: load, validate, in-memory model.

See §5 of ``docs/design/access-control.md`` for the schema.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Set, TYPE_CHECKING

import yaml

from skillberry_store.access_control.login_banner import (
    BANNER_MAX_CHARS,
    BANNER_MAX_LINES,
    LoginBanner,
    build_banner,
)

if TYPE_CHECKING:
    from skillberry_store.access_control.pdp import Subject

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_FILENAME = "access_control_config.yaml"

VALID_MODES = {"disabled", "standalone"}
RESERVED_MODES = {"delegated"}

# Known resources / verbs. Unknown values in a rule are dropped with a
# warning per §5.2.
KNOWN_RESOURCES = {
    "skills",
    "tools",
    "snippets",
    "vmcp_servers",
    "vnfs_servers",
    "admin",
    "plugins",
    "facets",
    "system",
    "*",
}
KNOWN_VERBS = {
    "list",
    "get",
    "create",
    "update",
    "delete",
    "search",
    "execute",
    "admin",
    "*",
}

# The three values `npx_publish` may take. Canonical spellings; the loader
# accepts YAML booleans and the usual truthy/falsy words and normalises to these.
NPX_PUBLISH_ALL = "true"
NPX_PUBLISH_NONE = "false"
NPX_PUBLISH_SELECTIVE = "selective"
NPX_PUBLISH_VALUES = (NPX_PUBLISH_ALL, NPX_PUBLISH_NONE, NPX_PUBLISH_SELECTIVE)

DEFAULT_SESSION_TTL_SECONDS = 43200  # 12h
# Tokens minted for a plugin's own outward calls are short-lived and
# refreshed on demand rather than held for a session's lifetime — see
# plugin-identity §8 ("a minted token is a real bearer token").
DEFAULT_PLUGIN_TOKEN_TTL_SECONDS = 900  # 15m

# Login-information message caps (§5 of docs/design/login-info.md). A block
# scalar makes a 40-line message easy to write by accident; an unbounded one
# would push the login form out of the viewport and bloat the injected HTML.
LOGIN_INFO_MAX_CHARS = 1024
LOGIN_INFO_MAX_LINES = 10

# `format:` selects between the plain message and the rich banner. The default
# is `plain`, so a config written before the rich layer existed resolves to
# exactly the value it always did (docs/design/login-banner.md §11).
LOGIN_INFO_FORMATS = ("plain", "rich")
DEFAULT_LOGIN_INFO_FORMAT = "plain"

# Truthy spellings accepted for config booleans, matching the convention
# already used for env-var flags elsewhere in the repo (``main.py``,
# ``tools/configure.py``). YAML parses bare ``true``/``false`` as bools, so
# this only ever catches quoted values.
_TRUTHY = ("true", "1", "yes", "on")


class AccessControlConfigError(Exception):
    """Hard-fail during config load (e.g. mode=standalone but file broken)."""


@dataclass
class User:
    username: str
    tenant_id: str
    password_hash: str
    groups: List[str] = field(default_factory=list)


@dataclass
class Rule:
    resources: List[str]
    verbs: List[str]


@dataclass
class Role:
    name: str
    rules: List[Rule] = field(default_factory=list)


@dataclass
class Subject:  # binding subject reference (kind/name), not the runtime Subject
    kind: str  # 'tenant' | 'group'
    name: str


@dataclass
class RoleBinding:
    name: str
    subjects: List[Subject]
    roles: List[str]
    # scope: parsed but not surfaced in step 1 (forward-compat). Kept as raw
    # mapping if present, else None.
    scope: Optional[dict] = None


@dataclass
class AccessControlConfig:
    mode: str = "disabled"
    unauthenticated_paths: List[str] = field(default_factory=list)
    # Publish skills for `npx skills add` (docs/design/npx.md §5.12). This is
    # the one setting that makes skill CONTENT — not just metadata — reachable
    # without a session, which is why it lives beside `unauthenticated_paths`
    # rather than in an env var: anyone reviewing this file's security posture
    # sees it in the same glance.
    #
    # Tri-state, not a boolean:
    #
    #   "true"       every visible skill is publishable
    #   "false"      nothing is; the /pub routes are not registered at all
    #   "selective"  each skill's own `npx_publish` flag decides (the DEFAULT)
    #
    # A skill's own flag is consulted ONLY under "selective" — `true` and `false`
    # are deliberate overrides, so an operator can publish or withdraw the whole
    # store without touching every manifest.
    #
    # "selective" is the default because it is both useful and conservative: a
    # store that has set nothing publishes nothing (a skill with no flag is not
    # published), yet turning one skill on is a per-skill decision rather than an
    # all-or-nothing switch.
    #
    # Deliberately independent of `mode`: a mode-dependent default — "on when
    # disabled, off when standalone" — is exactly the kind of subtlety that
    # surprises someone who later enables auth. Every combination is legal.
    npx_publish: str = NPX_PUBLISH_SELECTIVE
    users: List[User] = field(default_factory=list)
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    roles: List[Role] = field(default_factory=list)
    bindings: List[RoleBinding] = field(default_factory=list)
    # Canonical, already-sanitized login message; ``None`` means "show
    # nothing". The gate being off, a malformed/absent message, and any mode
    # other than ``standalone`` all collapse into ``None``, so no surface
    # needs a gate check of its own — see §5 of docs/design/login-info.md.
    #
    # In ``format: rich`` this is the *degraded* text — the same message with
    # the markup resolved away — which is what keeps ``sbs login`` and the
    # ``GET /auth/whoami`` 401 plain by construction, no matter how elaborate
    # the banner gets (docs/design/login-banner.md §8).
    login_info: Optional[str] = None
    # The parsed rich banner, set only under ``format: rich``. The UI renders
    # this when present and falls back to ``login_info`` when it is not; every
    # other surface ignores it entirely.
    login_info_banner: Optional[LoginBanner] = None
    # Deployment-wide fallback identity for plugin work that no per-plugin
    # owner covers (plugin-identity §5.1). Precedence is: per-plugin owner
    # recorded by PATCH /plugins/{name} → this → none, and P5 applies.
    plugin_owner_tenant: Optional[str] = None
    plugin_owner_groups: List[str] = field(default_factory=list)
    plugin_token_ttl_seconds: int = DEFAULT_PLUGIN_TOKEN_TTL_SECONDS

    # -------- lookups -------------------------------------------------- #
    def role(self, name: str) -> Optional[Role]:
        for r in self.roles:
            if r.name == name:
                return r
        return None

    def user(self, username: str) -> Optional[User]:
        for u in self.users:
            if u.username == username:
                return u
        return None

    def roles_for(self, subject: "Subject") -> List[str]:
        """Return the union of role names bound to this subject."""
        result: List[str] = []
        seen: Set[str] = set()
        tenant_id = subject.tenant_id
        groups = set(subject.groups or [])
        for b in self.bindings:
            if not _binding_matches(b, tenant_id, groups):
                continue
            for role_name in b.roles:
                if role_name not in seen:
                    seen.add(role_name)
                    result.append(role_name)
        return result

    def groups_for_tenant(self, tenant_id: str) -> List[str]:
        """Groups to attribute to ``tenant_id`` when building a Subject.

        A configured user's own groups win. Otherwise, the plugin owner
        tenant may declare groups without having a ``standalone.users``
        entry at all — that is the point of a *virtual* subject (§5.3): its
        grants are reviewable in the same YAML as every other grant, but no
        password hash exists for it anywhere, so it cannot be logged into
        from the network.
        """
        for u in self.users:
            if u.tenant_id == tenant_id:
                return list(u.groups or [])
        if tenant_id and tenant_id == self.plugin_owner_tenant:
            return list(self.plugin_owner_groups)
        return []

    def is_unauthenticated(self, method: str, path: str) -> bool:
        method = method.upper()
        for entry in self.unauthenticated_paths:
            m, p = _split_method_path(entry)
            if m is None or m == method:
                if _path_matches(p, path):
                    return True
        return False


def _binding_matches(
    b: RoleBinding, tenant_id: Optional[str], groups: Set[str]
) -> bool:
    for s in b.subjects:
        if s.kind == "tenant" and tenant_id is not None and s.name == tenant_id:
            return True
        if s.kind == "group" and s.name in groups:
            return True
    return False


def _split_method_path(entry: str) -> tuple[Optional[str], str]:
    """Parse ``'GET /health'`` into ``('GET', '/health')``; bare paths → (None, path)."""
    parts = entry.strip().split(None, 1)
    if len(parts) == 2 and parts[0].isalpha():
        return parts[0].upper(), parts[1]
    return None, entry.strip()


def _path_matches(pattern: str, path: str) -> bool:
    """Loose match: exact, or pattern with a trailing '*' as a prefix match."""
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
_DEFAULT_UNAUTH_PATHS = [
    "GET /health",
    "GET /health/ready",
    "GET /admin/metrics",
    "POST /auth/login",
    # Logout and whoami need to be reachable without RBAC: they self-resolve
    # the bearer token from the request. See §7.2 / §10.4.
    "POST /auth/logout",
    "GET /auth/whoami",
    "GET /docs",
    "GET /openapi.json",
    "GET /redoc",
    # Root redirect and SPA deep-links → /ui: public, no auth needed.
    # HEAD is listed alongside GET because the /ui handler serves both (the
    # audit requires *every* method on a route to be allow-listed).
    "GET /",
    "GET /ui*",
    "HEAD /ui*",
    # Every /control_sse* path is a Control MCP transport (SSE handshake +
    # JSON-RPC messages), including per-tenant mount points like
    # /control_sse/<username>. The tool invocations that the transport
    # forwards ARE gated by the enforce dependency on re-dispatch; the
    # transport itself is not (SSE mounts are Starlette ``Mount`` objects,
    # so router-level deps don't reach them either — the allow-list entry
    # is defensive belt-and-braces).
    "GET /control_sse*",
    "POST /control_sse*",
    # Per-skill discovery for `npx skills add` (docs/design/npx.md §4.3.8).
    # Read-only, one skill per URL, gated by `npx_publish` — when that is off
    # the routes are not registered at all and this entry matches nothing. The
    # path segment is a scoped capability token (standalone) or a slug
    # (disabled).
    # NOTE: `_path_matches` supports only a trailing '*' — a middle wildcard
    # like /pub/*/.well-known/* is inexpressible, so /pub/ MUST remain a
    # dedicated prefix with nothing else ever mounted under it (§5.10 #1).
    "GET /pub/*",
    # CLI download (docs/design/new_cli.md §5.5). A browser, `curl`, CI or a
    # freshly downloaded binary has no token to offer, and a user who cannot sign
    # in yet is exactly the user who wants the CLI. The surface serves no tenant
    # data, no configuration and no login message, and accepts no request body.
    # HEAD is listed alongside GET because the route serves both — the audit
    # requires every method on a route to be allow-listed — and because HEAD is
    # how a client reads the artifact's sha256 without transferring it.
    "GET /cli*",
    "HEAD /cli*",
]


def _effective_unauth_paths(configured: Optional[List[str]]) -> List[str]:
    """The allow-list the PEP enforces: the built-in defaults plus the config's.

    ``unauthenticated_paths`` in the config file **adds to** the defaults above;
    it does not replace them. That is what makes the defaults a dependable floor:
    every entry in them is either a probe that must answer before anyone can
    authenticate, an authentication endpoint itself, or a public read surface, and
    a deployment that lost one would be unable to boot, unable to log in, or
    unable to serve its own docs.

    Order is preserved with the defaults first, and duplicates are collapsed, so
    a config file that re-lists a default (all three shipped files do) is
    idempotent rather than additive.
    """
    merged: List[str] = []
    for entry in list(_DEFAULT_UNAUTH_PATHS) + list(configured or []):
        if entry not in merged:
            merged.append(entry)
    return merged


def _resolve_config_path(path: Optional[str]) -> str:
    return (
        path or os.environ.get("SBS_ACCESS_CONTROL_CONFIG") or DEFAULT_CONFIG_FILENAME
    )


def _resolve_session_ttl(yaml_value: Optional[int]) -> int:
    env_val = os.environ.get("SBS_SESSION_TTL")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            logger.warning("SBS_SESSION_TTL=%r is not an int; ignoring", env_val)
    if yaml_value is not None:
        try:
            return int(yaml_value)
        except (TypeError, ValueError):
            logger.warning(
                "standalone.session_ttl_seconds=%r is not an int; using default",
                yaml_value,
            )
    return DEFAULT_SESSION_TTL_SECONDS


def _resolve_plugin_token_ttl(yaml_value: Optional[int]) -> int:
    env_val = os.environ.get("SBS_PLUGIN_TOKEN_TTL")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            logger.warning("SBS_PLUGIN_TOKEN_TTL=%r is not an int; ignoring", env_val)
    if yaml_value is not None:
        try:
            return int(yaml_value)
        except (TypeError, ValueError):
            logger.warning(
                "plugins.token_ttl_seconds=%r is not an int; using default",
                yaml_value,
            )
    return DEFAULT_PLUGIN_TOKEN_TTL_SECONDS


def load_config(path: Optional[str] = None) -> AccessControlConfig:
    """Load and validate the access-control YAML.

    Returns an ``AccessControlConfig`` instance. Behavior:

    * Missing file, ``mode`` unset or ``disabled`` -> defaults (disabled).
    * Missing/broken file with ``mode: standalone`` -> hard fail
      (``AccessControlConfigError``), per §5.2 fail-closed principle.
    * Unknown resource/verb tokens in rules -> warn + drop rule, per §5.2.
    * ``mode: delegated`` -> hard fail with a clear "not yet supported" msg.
    * A top-level ``plugins:`` block supplies the deployment-wide owner
      tenant for plugin work (see ``plugin_owner_tenant``).
    """
    cfg_path = _resolve_config_path(path)

    if not os.path.isfile(cfg_path):
        logger.info(
            "Access-control config file not present at %s; using defaults "
            "(mode=disabled)",
            cfg_path,
        )
        return AccessControlConfig(
            mode="disabled",
            unauthenticated_paths=_effective_unauth_paths(None),
        )

    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as e:  # noqa: BLE001
        # Can't peek at mode; assume worst case -> fail closed if the file
        # exists but is broken.
        raise AccessControlConfigError(
            f"Failed to parse access-control config {cfg_path}: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise AccessControlConfigError(
            f"Access-control config {cfg_path} is not a mapping"
        )

    mode = raw.get("mode", "disabled")
    if mode in RESERVED_MODES:
        raise AccessControlConfigError(
            f"Access-control mode '{mode}' is reserved but not yet supported"
        )
    if mode not in VALID_MODES:
        # Malformed mode: fail-closed only if it wasn't 'disabled' by intent.
        raise AccessControlConfigError(
            f"Unknown access-control mode '{mode}' (valid: {sorted(VALID_MODES)})"
        )

    configured = raw.get("unauthenticated_paths")
    unauth_paths = _effective_unauth_paths(configured)
    extra = [
        entry for entry in (configured or []) if entry not in _DEFAULT_UNAUTH_PATHS
    ]
    if extra:
        logger.info(
            "Access-control: %s adds %d unauthenticated path pattern(s) beyond "
            "the defaults: %s",
            cfg_path,
            len(extra),
            ", ".join(extra),
        )
    npx_publish = _coerce_npx_publish(raw.get("npx_publish"), cfg_path)

    standalone = raw.get("standalone") or {}
    if not isinstance(standalone, dict):
        raise AccessControlConfigError(
            f"'standalone' block in {cfg_path} must be a mapping"
        )
    session_ttl = _resolve_session_ttl(standalone.get("session_ttl_seconds"))
    users = _parse_users(standalone.get("users") or [], cfg_path)
    login_info, login_info_banner = _parse_login_info(
        standalone.get("login_info"), cfg_path, mode
    )

    plugins_block = raw.get("plugins") or {}
    if not isinstance(plugins_block, dict):
        raise AccessControlConfigError(
            f"'plugins' block in {cfg_path} must be a mapping"
        )
    plugin_owner_tenant = plugins_block.get("owner_tenant")
    plugin_owner_tenant = (
        str(plugin_owner_tenant).strip() or None if plugin_owner_tenant else None
    )
    plugin_owner_groups = [str(g) for g in (plugins_block.get("owner_groups") or [])]
    plugin_token_ttl = _resolve_plugin_token_ttl(plugins_block.get("token_ttl_seconds"))

    roles = _parse_roles(raw.get("roles") or [], cfg_path)
    bindings = _parse_bindings(raw.get("bindings") or [], cfg_path)

    # Cross-reference validation.
    role_names = {r.name for r in roles}
    for b in bindings:
        for role_name in b.roles:
            if role_name not in role_names:
                if mode == "standalone":
                    raise AccessControlConfigError(
                        f"Binding '{b.name}' references unknown role '{role_name}'"
                    )
                logger.warning(
                    "Binding '%s' references unknown role '%s' (ignored in "
                    "disabled mode)",
                    b.name,
                    role_name,
                )

    cfg = AccessControlConfig(
        mode=mode,
        unauthenticated_paths=unauth_paths,
        npx_publish=npx_publish,
        users=users,
        session_ttl_seconds=session_ttl,
        roles=roles,
        bindings=bindings,
        login_info=login_info,
        login_info_banner=login_info_banner,
        plugin_owner_tenant=plugin_owner_tenant,
        plugin_owner_groups=plugin_owner_groups,
        plugin_token_ttl_seconds=plugin_token_ttl,
    )
    logger.info(
        "Loaded access-control config from %s: mode=%s, users=%d, roles=%d, "
        "bindings=%d",
        cfg_path,
        cfg.mode,
        len(cfg.users),
        len(cfg.roles),
        len(cfg.bindings),
    )
    return cfg


_FALSY = ("false", "0", "no", "off")


def _coerce_npx_publish(raw: Any, cfg_path: str) -> str:
    """Coerce ``npx_publish`` to one of :data:`NPX_PUBLISH_VALUES`.

    Read with ``raw.get(...)`` like every other known key, so a config file that
    predates the setting keeps loading — it resolves to ``selective``, under which
    a skill that has set no flag of its own is not published, so such a file
    publishes nothing until someone opts a skill in.

    YAML parses bare ``true``/``false`` as booleans, so both those and the usual
    quoted spellings are accepted and normalised. A junk value fails **closed**
    (``false``) rather than to the default: an operator who typos the value should
    get nothing published, not a mode they did not choose.
    """
    if isinstance(raw, bool):
        return NPX_PUBLISH_ALL if raw else NPX_PUBLISH_NONE
    if raw is None:
        return NPX_PUBLISH_SELECTIVE
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value == NPX_PUBLISH_SELECTIVE:
            return NPX_PUBLISH_SELECTIVE
        if value in _TRUTHY:
            return NPX_PUBLISH_ALL
        if value in _FALSY:
            return NPX_PUBLISH_NONE
    logger.warning(
        "npx_publish=%r in %s is not one of %s; treating as %r (fail-closed)",
        raw,
        cfg_path,
        ", ".join(NPX_PUBLISH_VALUES),
        NPX_PUBLISH_NONE,
    )
    return NPX_PUBLISH_NONE


def _coerce_login_info_enabled(raw: Any, cfg_path: str) -> bool:
    """Coerce ``login_info.enabled`` to a bool, warning on junk values."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, str) and raw.strip().lower() in _TRUTHY:
        return True
    logger.warning(
        "standalone.login_info.enabled=%r in %s is not a boolean; treating as " "false",
        raw,
        cfg_path,
    )
    return False


def _coerce_login_info_format(raw: Any, cfg_path: str) -> str:
    """Coerce ``login_info.format`` to ``plain`` or ``rich``.

    An unrecognized value falls back to ``plain`` rather than dropping the
    message: an operator who typos ``format: rish`` wants their text shown,
    just without the presentation.
    """
    if raw is None:
        return DEFAULT_LOGIN_INFO_FORMAT
    if isinstance(raw, str) and raw.strip().lower() in LOGIN_INFO_FORMATS:
        return raw.strip().lower()
    logger.warning(
        "standalone.login_info.format=%r in %s is not one of %s; treating as %r",
        raw,
        cfg_path,
        ", ".join(LOGIN_INFO_FORMATS),
        DEFAULT_LOGIN_INFO_FORMAT,
    )
    return DEFAULT_LOGIN_INFO_FORMAT


def _sanitize_login_info(
    message: str,
    cfg_path: str,
    max_chars: int = LOGIN_INFO_MAX_CHARS,
    max_lines: int = LOGIN_INFO_MAX_LINES,
) -> str:
    """Normalize, strip control characters from, and cap a login message.

    Steps 3-6 of §5 in docs/design/login-info.md. The result is safe to
    ``print()`` to a TTY: the only control character that survives is
    ``\n``, so a configured ANSI escape sequence cannot reposition the
    cursor or recolor the terminal.

    The caps are arguments because a rich message spends characters on markup
    (and possibly on an inline image) before any of them reach the reader, so
    it gets a larger budget — while the *degraded* text is re-capped at the
    plain limits, keeping the CLI's contract unchanged
    (docs/design/login-banner.md §5).
    """
    # Step 3: normalize line endings (a config edited on Windows carries CRLF).
    message = message.replace("\r\n", "\n").replace("\r", "\n")

    # Step 4: strip C0 and C1 control characters, allow-listing only "\n".
    # Tabs go too: they do nothing under CSS `pre-line` and nothing a space
    # cannot do in a terminal, so allowing them would widen the allow-list
    # for no gain.
    message = "".join(
        ch
        for ch in message
        if ch == "\n" or not ("\x00" <= ch <= "\x1f" or "\x7f" <= ch <= "\x9f")
    )

    # Step 5: cap length and line count, warning about whichever limit hit.
    if len(message) > max_chars:
        logger.warning(
            "standalone.login_info.message in %s exceeds the %d-character "
            "limit (%d); truncating",
            cfg_path,
            max_chars,
            len(message),
        )
        message = message[:max_chars]
    lines = message.split("\n")
    if len(lines) > max_lines:
        logger.warning(
            "standalone.login_info.message in %s exceeds the %d-line limit "
            "(%d); truncating",
            cfg_path,
            max_lines,
            len(lines),
        )
        message = "\n".join(lines[:max_lines])

    # Step 6.
    return message.strip()


def _parse_login_info(
    raw: Any, cfg_path: str, mode: str
) -> tuple[Optional[str], Optional[LoginBanner]]:
    """Resolve ``standalone.login_info`` to ``(plain message, rich banner)``.

    ``(None, None)`` is the only "off" state and every drop condition
    collapses into it, so the UI, CLI and REST surfaces carry no gate or mode
    checks of their own (§5 of docs/design/login-info.md).

    Under the default ``format: plain`` the second element is always ``None``
    and the first is byte-for-byte what it has always been. Under
    ``format: rich`` the message is parsed into a banner and the first element
    becomes its plain-text degradation — so every surface but the UI keeps
    receiving a plain string, and the CLI needs no knowledge of the rich
    layer at all (docs/design/login-banner.md §8).

    Never raises. A banner must not be able to stop the server from booting,
    so every malformed shape is warned about and dropped rather than turned
    into an ``AccessControlConfigError`` — the warn-and-drop half of §5.2 of
    docs/design/access-control.md, not its fail-closed half.
    """
    # Step 1: only `standalone` has an in-store login to annotate. A block
    # present in a `disabled` config is normal — the same file is routinely
    # shared across deployments that differ only in `mode` — so it is
    # debug-logged, not warned.
    if mode != "standalone":
        if raw is not None:
            logger.debug(
                "Ignoring standalone.login_info in %s: mode=%s has no in-store "
                "login",
                cfg_path,
                mode,
            )
        return None, None

    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        logger.warning(
            "standalone.login_info in %s must be a mapping, got %r; ignoring",
            cfg_path,
            raw,
        )
        return None, None

    enabled = _coerce_login_info_enabled(raw.get("enabled"), cfg_path)
    message = raw.get("message")

    if message is not None and not isinstance(message, str):
        logger.warning(
            "standalone.login_info.message in %s must be a string, got %r; " "ignoring",
            cfg_path,
            message,
        )
        message = None

    if not enabled:
        if message:
            # The steady state for a message staged ahead of being switched
            # on. Setting the text and enabling its display are deliberately
            # independent controls (§4.1), so this must not be noisy.
            logger.debug(
                "standalone.login_info.message is set in %s but enabled is "
                "false; not showing it",
                cfg_path,
            )
        return None, None

    fmt = _coerce_login_info_format(raw.get("format"), cfg_path)
    if fmt == "rich":
        resolved = _parse_rich_login_info(message, raw.get("style"), cfg_path)
        if resolved is not None:
            return resolved
        # Nothing renderable came out of the markup. Fall through to the plain
        # path so the operator's text is still shown -- and so the "enabled but
        # empty" warning below is reached when there was no text either.
    elif raw.get("style") is not None:
        # Not a warning: a `style:` block staged next to `format: plain` is the
        # same "set it before switching it on" pattern as `enabled` (§4.1).
        logger.debug(
            "standalone.login_info.style in %s is ignored under format=%s",
            cfg_path,
            fmt,
        )

    sanitized = _sanitize_login_info(message, cfg_path) if message else ""
    if not sanitized:
        logger.warning(
            "standalone.login_info.enabled is true in %s but the message is "
            "missing or empty after sanitization; no login message will be "
            "shown",
            cfg_path,
        )
        return None, None
    return sanitized, None


def _parse_rich_login_info(
    message: Optional[str], raw_style: Any, cfg_path: str
) -> Optional[tuple[Optional[str], LoginBanner]]:
    """Parse a ``format: rich`` message into ``(plain text, banner)``.

    ``None`` -- as opposed to a tuple whose first element is ``None`` -- means
    the markup produced nothing renderable, and the caller should treat the
    message as plain. A tuple with a ``None`` first element is the legitimate
    case of a banner that is *only* presentation (a bare logo image, say):
    there the UI has something to draw and the CLI correctly has nothing to
    print.
    """
    if not message:
        return None
    # The rich caps apply to the source markup; the control-character strip is
    # the same pass the plain path uses, so a configured escape sequence still
    # cannot reach a terminal through the degraded text.
    source = _sanitize_login_info(message, cfg_path, BANNER_MAX_CHARS, BANNER_MAX_LINES)
    banner = build_banner(source, raw_style, cfg_path) if source else None
    if banner is None:
        logger.warning(
            "standalone.login_info in %s has format=rich but the message "
            "produced no renderable content; treating it as plain text",
            cfg_path,
        )
        return None
    # Re-capped at the plain limits: what the CLI is shown must not grow just
    # because the UI banner did.
    plain = _sanitize_login_info(banner.to_plain_text(), cfg_path)
    return (plain or None), banner


def _parse_users(raw_users: Iterable, cfg_path: str) -> List[User]:
    users: List[User] = []
    for raw in raw_users:
        if not isinstance(raw, dict):
            logger.warning("Skipping non-mapping user entry in %s: %r", cfg_path, raw)
            continue
        username = raw.get("username")
        password_hash = raw.get("password_hash")
        if not username or not password_hash:
            logger.warning(
                "Skipping user with missing username/password_hash in %s: %r",
                cfg_path,
                raw,
            )
            continue
        users.append(
            User(
                username=str(username),
                tenant_id=str(raw.get("tenant_id") or username),
                password_hash=str(password_hash),
                groups=list(raw.get("groups") or []),
            )
        )
    return users


def _parse_roles(raw_roles: Iterable, cfg_path: str) -> List[Role]:
    roles: List[Role] = []
    for raw in raw_roles:
        if not isinstance(raw, dict):
            logger.warning("Skipping non-mapping role entry in %s: %r", cfg_path, raw)
            continue
        name = raw.get("name")
        if not name:
            logger.warning("Skipping role with no name in %s: %r", cfg_path, raw)
            continue
        rules: List[Rule] = []
        for raw_rule in raw.get("rules") or []:
            if not isinstance(raw_rule, dict):
                continue
            resources = _filter_tokens(
                raw_rule.get("resources") or [],
                KNOWN_RESOURCES,
                f"role '{name}'",
                "resource",
            )
            verbs = _filter_tokens(
                raw_rule.get("verbs") or [],
                KNOWN_VERBS,
                f"role '{name}'",
                "verb",
            )
            if not resources or not verbs:
                continue
            rules.append(Rule(resources=resources, verbs=verbs))
        roles.append(Role(name=str(name), rules=rules))
    return roles


def _filter_tokens(
    tokens: Iterable, allowed: Set[str], context: str, kind: str
) -> List[str]:
    result: List[str] = []
    for t in tokens:
        if t in allowed:
            result.append(t)
        else:
            logger.warning("Dropping unknown %s '%s' in %s", kind, t, context)
    return result


def _parse_bindings(raw_bindings: Iterable, cfg_path: str) -> List[RoleBinding]:
    bindings: List[RoleBinding] = []
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            logger.warning(
                "Skipping non-mapping binding entry in %s: %r", cfg_path, raw
            )
            continue
        name = raw.get("name") or f"binding-{len(bindings)}"
        subjects_raw = raw.get("subjects") or []
        subjects: List[Subject] = []
        for s in subjects_raw:
            if not isinstance(s, dict):
                continue
            kind = s.get("kind")
            s_name = s.get("name")
            if kind not in ("tenant", "group") or not s_name:
                logger.warning("Skipping invalid subject in binding '%s': %r", name, s)
                continue
            subjects.append(Subject(kind=str(kind), name=str(s_name)))
        roles = [str(r) for r in (raw.get("roles") or [])]
        scope = raw.get("scope")  # parsed but ignored in step 1
        bindings.append(
            RoleBinding(
                name=str(name),
                subjects=subjects,
                roles=roles,
                scope=scope if isinstance(scope, dict) else None,
            )
        )
    return bindings


# --------------------------------------------------------------------------- #
# Cached loader (mirrors endpoint_auth.get_config).
# --------------------------------------------------------------------------- #
_config_cache: Optional[AccessControlConfig] = None
_config_path_loaded: Optional[str] = None


def get_config(path: Optional[str] = None) -> AccessControlConfig:
    """Return the cached access-control config, loading it on first use."""
    global _config_cache, _config_path_loaded
    cfg_path = _resolve_config_path(path)
    if _config_cache is None or _config_path_loaded != cfg_path:
        _config_cache = load_config(cfg_path)
        _config_path_loaded = cfg_path
    return _config_cache


def reset_config_cache() -> None:
    global _config_cache, _config_path_loaded
    _config_cache = None
    _config_path_loaded = None
