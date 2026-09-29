package cli

// Build-time injected values and the padded URL slot.
//
// Both mechanisms in docs/design/new_cli.md §5.2 write to the variables in this
// file: `rebuild` passes
//
//	-ldflags -X github.com/skillberry-ai/skillberry-store/client/go/cli.URLSlot=<url>
//
// and `patch` rewrites the slot's bytes in place inside an already-linked
// artifact.
//
// NOTE the `-X` target is the full *import path*, not `main`. These variables
// live in the importable `cli` package rather than in `main` — which is what
// lets the tests live in client/go/tests — and `-X` addresses a variable by
// package path. A stale `-X main.URLSlot=…` is accepted silently by the linker
// and does nothing at all, which is the same failure mode as the constant-
// initializer gotcha below. Every caller is listed in
// client/go/build.sh, .mk/dev.mk, services/cli_artifacts.py and the
// cli-artifacts workflow; they must agree.
//
// GOTCHA (§3.4 #1, G8): `-ldflags -X` silently no-ops unless the target variable
// is a package-level string with a *constant* initializer. An expression such as
// `"…" + strings.Repeat("#", 0)` is enough to make the linker ignore the flag
// without complaining: the binary keeps its source default and nothing fails
// loudly. So:
//
//   - every value here MUST be a plain string literal, never an expression;
//   - `TestURLSlotIsPatchable` asserts the slot keeps its padded shape, and
//     the build script's control case (a binary linked against a dead port
//     that must FAIL) is what actually proves the flag took effect.
//
// Do not "tidy" these into a const block: `-X` cannot write to a const.

// URLSlot is the compiled-in store URL, padded to SlotWidth with '#' so the
// `patch` mechanism can rewrite it in place without changing the file size.
//
// The padding is what makes patching possible at all: a linked binary has no
// room to grow a string, but overwriting SlotWidth bytes and re-trimming the
// padding at runtime costs zero bytes. See services/cli_artifacts.py's
// patch_url_slot for the writer.
var URLSlot = "http://localhost:8000#######################################"

// Version is the CLI version, injected at build time by `-ldflags -X`.
// "dev" is what a plain `go build` reports, which is the honest answer for a
// developer's own build.
var Version = "dev"

// EngineVersion records which restish the artifact embeds. It is part of the
// preparation stamp key (§5.3), so it is injected rather than inferred: the
// server has to know what it is serving without executing it.
var EngineVersion = "unknown"

// SlotWidth is the exact byte length of URLSlot's initializer above. The
// `patch` mechanism refuses to write a URL longer than this, and refuses to
// write one that would change the file size — both checked against this
// constant on the Go side and against the located slot on the Python side.
const SlotWidth = 60

// SlotPad is the padding byte. Chosen as '#' because it cannot appear in a URL
// that passes the §5.7 validator, so trimming it can never truncate a real URL.
const SlotPad = '#'
