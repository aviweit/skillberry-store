package tests

import (
	"errors"
	"strings"
	"testing"

	"github.com/skillberry-ai/skillberry-store/client/go/cli"
)

// §8.1 #6's static half. The dynamic half — asserting on a *built binary's*
// help output — lives in the pytest end-to-end test and in CI (§5.10 step 2),
// because only a real binary can prove the surface restish actually renders.

// knownRestishStrings is the §3.3 inventory: every place the word survives in
// user-visible output, measured against a live store.
//
// Kept here as data, and asserted to be exactly this long, so that an upstream
// fix or an upstream regression both show up as a failing test rather than as
// nobody noticing. See docs/design/new_cli.md §3.3 for each entry's resolution.
var knownRestishStrings = []string{
	// Two global flag descriptions with no public hook; upstream PR pending (G1).
	"--help-all",
	"--rsh-config",
	// `doctor`'s version label and its shell-setup hint (G2). Moved off the
	// primary surface by SupportCommandNamespace, so they only appear under
	// `sbs cli doctor`.
	"Restish cli.Version:",
	"restish shell setup",
}

func TestKnownRestishStringInventoryIsExact(t *testing.T) {
	// The count is the assertion. If upstream merges the branding PR, this fails
	// and someone deletes an entry; if upstream regresses and adds a fifth
	// mention, the e2e help assertion fails and someone adds one here
	// deliberately. Either way the drift is visible at PR time.
	if len(knownRestishStrings) != 4 {
		t.Errorf("the §3.3 inventory has %d entries, want exactly 4; "+
			"update docs/design/new_cli.md §3.3 and the e2e allowlist together",
			len(knownRestishStrings))
	}
}

// The root description is ours, not restish's — which is what D2 means by
// "we never emit it" rather than "we rewrite it".
func TestLongHelpIsBrandedAndSuggestsOnlyRealCommands(t *testing.T) {
	help := cli.LongHelp()

	if strings.Contains(strings.ToLower(help), "restish") {
		t.Error("the root description must not mention restish")
	}
	if !strings.Contains(help, "Skillberry Store") {
		t.Error("the root description should name the product")
	}

	// Goal, §1: "Every command the CLI *suggests* is a command that works when
	// typed." These are the commands this text tells the user to run.
	for _, cmd := range []string{
		cli.CLIName + " list-skills",
		cli.CLIName + " connect <url>",
		cli.CLIName + " download-cli",
		cli.CLIName + " self-update",
		cli.CLIName + " " + cli.SupportNamespace + " doctor",
		cli.CLIName + " " + cli.SupportNamespace + " config path",
	} {
		if !strings.Contains(help, cmd) {
			t.Errorf("help does not mention %q", cmd)
		}
	}

	// The two-level shape (`sbs sbs list-skills`) was the shim's core problem;
	// a promoted surface must never produce it.
	if strings.Contains(help, cli.CLIName+" "+cli.CLIName+" ") {
		t.Error("help contains a doubled command name, which a promoted surface must never produce")
	}
	// `sbs api connect` is restish's shape, not ours, and does not exist here.
	if strings.Contains(help, cli.CLIName+" api ") {
		t.Errorf("help suggests `%s api ...`, which does not exist in a promoted surface", cli.CLIName)
	}

	// Both client-side env vars are documented where a user will look.
	for _, v := range []string{cli.URLEnvVar, cli.TokenEnvVar} {
		if !strings.Contains(help, v) {
			t.Errorf("help does not document %s", v)
		}
	}
}

func TestShortHelpIsBranded(t *testing.T) {
	if strings.Contains(strings.ToLower(cli.ShortHelp), "restish") {
		t.Errorf("cli.ShortHelp = %q, must not mention restish", cli.ShortHelp)
	}
}

// restish renders "<command> version <value>", so the value must not repeat the
// name or `sbs --version` prints "sbs version sbs 0.1.0".
func TestVersionLineDoesNotRepeatCommandName(t *testing.T) {
	got := cli.VersionLine()
	if strings.HasPrefix(got, cli.CLIName) {
		t.Errorf("cli.VersionLine() = %q; restish prefixes it with the command name already", got)
	}
	// The engine version belongs in a bug report, so it is deliberately present
	// — this is the one "restish" mention we emit on purpose.
	if !strings.Contains(got, "restish") {
		t.Errorf("cli.VersionLine() = %q, want the embedded engine cli.Version for bug reports", got)
	}
}

// ---------------------------------------------------------------------------
// The baked default config
// ---------------------------------------------------------------------------

func TestDefaultConfigRegistersPromotedAPI(t *testing.T) {
	cfg := cli.DefaultConfig(cli.MapEnviron{cli.URLEnvVar: "http://store.test:8000"})

	api := cfg.APIs[cli.APIName]
	if api == nil {
		t.Fatalf("default config has no %q API; the promoted surface requires it "+
			"(SetCommandSurface names this key)", cli.APIName)
	}
	if api.BaseURL != "http://store.test:8000" {
		t.Errorf("base URL = %q, want the resolved URL", api.BaseURL)
	}
	// Without a spec URL a promoted API can build no commands at all.
	if api.SpecURL != "http://store.test:8000/openapi.json" {
		t.Errorf("spec URL = %q, want the derived openapi.json URL", api.SpecURL)
	}
}

func TestDefaultConfigProfiles(t *testing.T) {
	cfg := cli.DefaultConfig(cli.MapEnviron{})
	api := cfg.APIs[cli.APIName]

	def := api.Profiles["default"]
	if def == nil || def.Auth == nil || def.Auth.Type != cli.AuthSchemeName {
		t.Fatalf("default profile must select the %q handler, got %+v", cli.AuthSchemeName, def)
	}

	// The CI/scripting path: `env:SBS_TOKEN` as a literal in the baked config,
	// which is how the shim's `env-token` profile worked — now with no
	// subprocess call to create it.
	envProfile := api.Profiles["env-token"]
	if envProfile == nil {
		t.Fatal("the env-token profile is missing; SBS_TOKEN would not work")
	}
	joined := strings.Join(envProfile.Headers, "\n")
	if !strings.Contains(joined, cli.TokenEnvVar) {
		t.Errorf("env-token headers = %v, want a reference to %s", envProfile.Headers, cli.TokenEnvVar)
	}
	if !strings.Contains(joined, "Authorization") {
		t.Errorf("env-token headers = %v, want an Authorization header", envProfile.Headers)
	}
	// The token must be referenced, never inlined: a literal here would be a
	// credential compiled into the artifact.
	if strings.Contains(joined, "Bearer "+cli.TokenEnvVar) {
		t.Error("the token env var name must be interpolated, not concatenated literally")
	}
}

// The reserved names upstream turns into a startup panic/error if a generated
// operation collides with them (§4.2). Mirrored by a pytest check over the
// OpenAPI spec so a colliding x-cli-name fails at PR time, not at release time.
func TestSupportNamespaceIsNotAnOperationName(t *testing.T) {
	if cli.SupportNamespace == "" {
		t.Fatal("a support namespace is required; otherwise the residual §3.3 " +
			"strings land on the primary help screen")
	}
	// A sanity bound: the namespace has to be a plausible command token.
	if strings.ContainsAny(cli.SupportNamespace, " /-") {
		t.Errorf("cli.SupportNamespace = %q is not a single command token", cli.SupportNamespace)
	}
}

// ---------------------------------------------------------------------------
// cli.WrapRunError — §G3, the offline promoted-root failure
// ---------------------------------------------------------------------------

func TestWrapRunErrorExplainsSpecDiscoveryFailure(t *testing.T) {
	raw := errors.New(`generated commands for promoted API "store" are unavailable: spec discovery failed: GET http://store.test/openapi.json: connection refused`)
	got := cli.WrapRunError(raw, "http://store.test")
	if got == nil {
		t.Fatal("cli.WrapRunError returned nil for a real error")
	}
	msg := got.Error()

	// The single most likely first-run failure for a downloaded artifact. The
	// raw upstream text names no remedy, so the wrapper must name all three.
	if !strings.Contains(msg, "http://store.test") {
		t.Errorf("message does not name the configured URL: %q", msg)
	}
	for _, remedy := range []string{cli.CLIName + " connect", cli.URLEnvVar, cli.CLIName + " download-cli"} {
		if !strings.Contains(msg, remedy) {
			t.Errorf("message does not offer %q: %q", remedy, msg)
		}
	}
	// The original text is kept: it carries the actual network error.
	if !strings.Contains(msg, "connection refused") {
		t.Errorf("message drops the underlying cause: %q", msg)
	}
}

func TestWrapRunErrorPassesOtherErrorsThrough(t *testing.T) {
	// Attaching store-URL advice to a genuine 404 or a validation error would be
	// actively misleading, so only spec-discovery failures are wrapped.
	raw := errors.New("skill not found")
	got := cli.WrapRunError(raw, "http://store.test")
	if got == nil || got.Error() != "skill not found" {
		t.Errorf("cli.WrapRunError = %v, want the original error untouched", got)
	}
	if cli.WrapRunError(nil, "http://store.test") != nil {
		t.Error("cli.WrapRunError(nil) must stay nil")
	}
}

// ---------------------------------------------------------------------------
// cli.ExitCode
// ---------------------------------------------------------------------------

func TestNewExitCodeZeroIsNil(t *testing.T) {
	// A verb returning 0 must not surface as a non-nil error, or every success
	// would print an error line and exit 1.
	if err := cli.NewExitCode(0); err != nil {
		t.Errorf("cli.NewExitCode(0) = %v, want nil", err)
	}
	err := cli.NewExitCode(2)
	if err == nil {
		t.Fatal("cli.NewExitCode(2) must be non-nil")
	}
	var ec cli.ExitCode
	if !errors.As(err, &ec) || int(ec) != 2 {
		t.Errorf("cli.NewExitCode(2) did not round-trip through errors.As, got %v", err)
	}
}
