#!/usr/bin/env bash
#
# Cross-compile the native `sbs` CLI for every supported platform and emit a
# manifest of what was built.
#
# This is the CI step of docs/design/new_cli.md §5.10 #1 and the `rebuild`
# mechanism of §5.2. Because Go cross-compiles with CGO_ENABLED=0, ONE Linux
# runner produces all five artifacts (M4); no per-OS runner is needed to build
# them, only to execute them.
#
# Usage:
#   client/go/build.sh [--out DIR] [--url URL] [--version V] [--platforms "a b c"]
#
# The emitted prebuilt-manifest.json is what the server's artifact service reads
# to learn each artifact's sha256 and slot offsets without executing it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The Go module root. The importable library is $GO_ROOT/cli and the binary's
# entry point is $GO_ROOT/cli/cmd/sbs -- see the note in cli/version.go for why
# the two are separate packages.
GO_ROOT="$REPO_ROOT/client/go"
CLI_PKG="github.com/skillberry-ai/skillberry-store/client/go/cli"
CMD_PKG="./cli/cmd/sbs"

# The closed platform enum of §5.1. Ids are <goos>-<goarch> and the server
# validates against this same list, so adding one here is not enough on its own.
DEFAULT_PLATFORMS="linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64"

OUT_DIR="$REPO_ROOT/cli-prebuilt"
# The compiled-in default URL. Left as the source default unless asked: a CI
# build is generic, and the per-deployment URL is injected later by the server's
# `patch` or `rebuild` step, not baked here.
BAKE_URL=""
VERSION=""
PLATFORMS="$DEFAULT_PLATFORMS"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)       OUT_DIR="$2"; shift 2 ;;
        --url)       BAKE_URL="$2"; shift 2 ;;
        --version)   VERSION="$2"; shift 2 ;;
        --platforms) PLATFORMS="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "build.sh: unknown option $1" >&2; exit 2 ;;
    esac
done

# Which toolchain to use. `make cli-dist` passes GO explicitly so a direct run
# and a make-driven one agree; standalone, this resolves the same way the
# makefile does.
GO="${GO:-go}"
if ! command -v "$GO" >/dev/null 2>&1; then
    if [[ -n "${GOROOT:-}" && -x "$GOROOT/bin/go" ]]; then
        GO="$GOROOT/bin/go"
    else
        for _c in /usr/local/go/bin/go /opt/go/bin/go /opt/homebrew/bin/go \
                  "$HOME/.local/go/bin/go" "$HOME/go/bin/go"; do
            if [[ -x "$_c" ]]; then GO="$_c"; break; fi
        done
    fi
fi

if ! command -v "$GO" >/dev/null 2>&1; then
    echo "build.sh: no Go toolchain found (tried '$GO', PATH, GOROOT and the usual locations)." >&2
    echo "  Point at one with GO=/path/to/go, or set GOROOT." >&2
    echo "  The runtime image does not need one (the default 'patch' mechanism," >&2
    echo "  §5.4 option A, rewrites a CI-built artifact in place). Building from" >&2
    echo "  source does. Install Go >= 1.25 and retry." >&2
    exit 1
fi

# Derive the version from git when not supplied, matching how the repo stamps
# every other artifact (docs/design/build_concepts.md).
if [[ -z "$VERSION" ]]; then
    if git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
        VERSION="0.0.0+g$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    else
        VERSION="dev"
    fi
fi

# The engine version comes from go.mod rather than from a second place that
# could disagree with what is actually linked in. It is part of the server's
# preparation stamp key (§5.3), so it has to be the truth.
#
# Reported, not discarded, when it cannot be read. `2>/dev/null | sed` under the
# `set -o pipefail` above is a trap: pipefail hands the pipeline go's non-zero
# status, `set -e` kills the script on the assignment, and the `:-unknown`
# fallback on the next line never runs -- so the build died with no output at
# all. The usual cause is a toolchain older than go.mod's `go` directive under
# GOTOOLCHAIN=local, which is a fixable local condition and worth naming.
# stderr is folded into the captured value so a failure carries go's own
# diagnostic; on success `go list -m -f {{.Version}}` writes only the version.
if ! ENGINE_VERSION="$(cd "$GO_ROOT" && "$GO" list -m -f '{{.Version}}' \
        github.com/rest-sh/restish/v2 2>&1)"; then
    echo "build.sh: cannot resolve the restish module version from $GO_ROOT/go.mod:" >&2
    printf '  %s\n' "$ENGINE_VERSION" >&2
    echo "  This is also what every per-platform build below would fail on, so it" >&2
    echo "  stops here. If the message above is about the Go version, either" >&2
    echo "  install the toolchain go.mod asks for and point at it with" >&2
    echo "  GO=/path/to/go, or allow Go to fetch it with GOTOOLCHAIN=auto." >&2
    exit 1
fi
ENGINE_VERSION="${ENGINE_VERSION#v}"
if [[ -z "$ENGINE_VERSION" ]]; then
    echo "build.sh: the restish module resolved to an empty version." >&2
    echo "  It is part of the server's preparation stamp key (§5.3) and cannot" >&2
    echo "  be guessed, so this is a hard failure rather than an 'unknown'." >&2
    exit 1
fi

# §5.7 / §7.2 / B14: a URL that reaches a linker flag is argument injection into
# our own build, so it is validated before it is used and never passed through a
# shell. The grammar is identical to the Go and Python validators.
if [[ -n "$BAKE_URL" ]]; then
    if ! [[ "$BAKE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]]; then
        echo "build.sh: refusing to bake an unacceptable URL: $BAKE_URL" >&2
        exit 2
    fi
    # The slot is fixed-width; a longer URL cannot be represented and must not be
    # silently truncated.
    if [[ ${#BAKE_URL} -gt 60 ]]; then
        echo "build.sh: URL is ${#BAKE_URL} bytes, which exceeds the 60-byte slot" >&2
        exit 2
    fi
fi

mkdir -p "$OUT_DIR"
# Resolved to an absolute path: the per-platform builds below `cd` into the Go
# module root, where a relative --out would no longer point anywhere.
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

echo "==> Building sbs $VERSION (engine restish $ENGINE_VERSION)"
echo "    platforms: $PLATFORMS"
echo "    output:    $OUT_DIR"
[[ -n "$BAKE_URL" ]] && echo "    baked URL: $BAKE_URL"

entries=()

for platform in $PLATFORMS; do
    goos="${platform%%-*}"
    goarch="${platform#*-}"

    filename="sbs"
    [[ "$goos" == "windows" ]] && filename="sbs.exe"

    dest_dir="$OUT_DIR/$platform"
    mkdir -p "$dest_dir"
    dest="$dest_dir/$filename"

    # -s -w strips the symbol table and DWARF (M7: 30-33 MB per artifact).
    # -trimpath keeps build paths out of the binary, so builds are comparable
    # and no developer's home directory ships to users (§7.4).
    # The -X target is the full import path of the package holding the variable,
    # not `main`: these live in the importable `cli` package. A stale
    # `-X main.version=...` is accepted silently by the linker and injects
    # nothing -- see the note in cli/version.go.
    ldflags="-s -w -X $CLI_PKG.Version=$VERSION -X $CLI_PKG.EngineVersion=$ENGINE_VERSION"
    if [[ -n "$BAKE_URL" ]]; then
        # An argv element, never a shell string: see B14.
        ldflags="$ldflags -X $CLI_PKG.URLSlot=$BAKE_URL"
    fi

    printf '    %-16s ' "$platform"
    start=$(date +%s)
    (
        cd "$GO_ROOT"
        CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
            "$GO" build -trimpath -ldflags "$ldflags" -o "$dest" "$CMD_PKG"
    )
    elapsed=$(( $(date +%s) - start ))

    size=$(wc -c < "$dest" | tr -d ' ')
    sha=$(sha256sum "$dest" | cut -d' ' -f1)
    echo "ok  ${size} bytes  ${elapsed}s  ${sha:0:12}"

    entries+=("$(printf '{"platform":"%s","filename":"%s","size":%s,"sha256":"%s"}' \
        "$platform" "$filename" "$size" "$sha")")
done

# The manifest the image build and the server's artifact service consume. Written
# last and atomically, the same discipline §5.3 requires of the served manifest.
manifest="$OUT_DIR/prebuilt-manifest.json"
{
    printf '{\n'
    printf '  "cli_name": "sbs",\n'
    printf '  "cli_version": "%s",\n' "$VERSION"
    printf '  "engine": {"name": "restish", "version": "%s", "license": "MIT"},\n' "$ENGINE_VERSION"
    printf '  "baked_url": %s,\n' "$([[ -n "$BAKE_URL" ]] && printf '"%s"' "$BAKE_URL" || printf 'null')"
    printf '  "slot_width": 60,\n'
    printf '  "generated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "artifacts": [\n'
    for i in "${!entries[@]}"; do
        printf '    %s' "${entries[$i]}"
        [[ $i -lt $(( ${#entries[@]} - 1 )) ]] && printf ','
        printf '\n'
    done
    printf '  ]\n'
    printf '}\n'
} > "$manifest.tmp"
mv "$manifest.tmp" "$manifest"

# restish is MIT and we redistribute it, so its licence ships beside the
# artifacts and goes into every archive; the repository's own LICENSE carries it
# too, under Third-party software notices (§7.4).
#
# Both spellings are tried: upstream ships LICENSE.md at v2.3.0, but a plain
# LICENSE is the more common convention and a future release could switch.
# Shipping no licence while redistributing an MIT binary is the one outcome that
# is not acceptable, so a miss is a hard failure rather than a warning.
engine_dir="$(cd "$GO_ROOT" && "$GO" list -m -f '{{.Dir}}' github.com/rest-sh/restish/v2)"
engine_license=""
for candidate in LICENSE.md LICENSE LICENSE.txt COPYING; do
    if [[ -f "$engine_dir/$candidate" ]]; then
        engine_license="$engine_dir/$candidate"
        break
    fi
done
if [[ -n "$engine_license" ]]; then
    # `install`, not `cp`: the module cache is read-only, so a plain copy lands
    # as mode 0444 and the *next* run cannot overwrite it -- `make cli-dist`
    # twice in a row failed on "Permission denied" after every artifact had
    # already been built. install replaces the destination and sets the mode.
    install -m 0644 "$engine_license" "$OUT_DIR/LICENSE.restish"
    echo "==> Bundled $(basename "$engine_license") as LICENSE.restish"
else
    echo "build.sh: could not find restish's licence under $engine_dir." >&2
    echo "  We redistribute restish, so shipping without its MIT licence is not an option." >&2
    exit 1
fi

echo "==> Wrote $manifest"
