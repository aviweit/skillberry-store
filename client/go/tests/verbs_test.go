package tests

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/skillberry-ai/skillberry-store/client/go/cli"
)

// devNull gives the verbs a real *os.File to write to without polluting the
// test output. The verbs take *os.File (not io.Writer) because they are also
// the process's own stdout/stderr in main().
func devNull(t *testing.T) *os.File {
	t.Helper()
	f, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return f
}

// captureFile gives a temp file the verbs can write to, plus a reader for it.
func captureFile(t *testing.T) (*os.File, func() string) {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "capture-*")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return f, func() string {
		body, _ := os.ReadFile(f.Name())
		return string(body)
	}
}

func testPaths(t *testing.T) cli.BrandedPaths {
	t.Helper()
	dir := t.TempDir()
	return cli.BrandedPaths{
		ConfigDir:  filepath.Join(dir, "sbs"),
		ConfigFile: filepath.Join(dir, "sbs", "sbs.json"),
		CacheDir:   filepath.Join(dir, "cache"),
	}
}

func readConfig(t *testing.T, path string) map[string]any {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("config not written: %v", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(body, &cfg); err != nil {
		t.Fatalf("config is not valid JSON (%q): %v", body, err)
	}
	return cfg
}

func TestConnectWritesBaseAndSpecURL(t *testing.T) {
	paths := testPaths(t)
	out, read := captureFile(t)

	if code := cli.DoConnect([]string{"http://store.test:8000/"}, out, devNull(t), paths); code != 0 {
		t.Fatalf("cli.DoConnect = %d, want 0", code)
	}

	cfg := readConfig(t, paths.ConfigFile)
	apis, _ := cfg["apis"].(map[string]any)
	entry, _ := apis[cli.APIName].(map[string]any)
	if entry["base_url"] != "http://store.test:8000" {
		t.Errorf("base_url = %v, want the trailing slash stripped", entry["base_url"])
	}
	// The spec URL matters as much as the base: a promoted API cannot build any
	// command without it, so writing one without the other yields a CLI that
	// connects but has no commands.
	if entry["spec_url"] != "http://store.test:8000/openapi.json" {
		t.Errorf("spec_url = %v, want the derived openapi.json URL", entry["spec_url"])
	}
	if !strings.Contains(read(), "http://store.test:8000") {
		t.Errorf("stdout = %q, want it to confirm the URL", read())
	}
}

func TestConnectWritesPrivateConfig(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permissions")
	}
	paths := testPaths(t)
	if code := cli.DoConnect([]string{"http://store.test"}, devNull(t), devNull(t), paths); code != 0 {
		t.Fatalf("cli.DoConnect = %d", code)
	}
	info, err := os.Stat(paths.ConfigFile)
	if err != nil {
		t.Fatal(err)
	}
	// restish refuses to read a group/world-readable config, and the file can
	// hold a bearer token. 0600 is both a security and a functional requirement.
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("config mode = %o, want 600", perm)
	}
}

func TestConnectPreservesOtherConfigKeys(t *testing.T) {
	paths := testPaths(t)
	if err := os.MkdirAll(paths.ConfigDir, 0o700); err != nil {
		t.Fatal(err)
	}
	// A user's theme, another API, and a hand-added profile on our own API.
	existing := `{
      "theme": {"keyword": "#abcdef"},
      "apis": {
        "other": {"base_url": "http://other.test"},
        "store": {"base_url": "http://old.test", "profiles": {"mine": {"headers": ["X-A: 1"]}}}
      }
    }`
	if err := os.WriteFile(paths.ConfigFile, []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}

	if code := cli.DoConnect([]string{"http://new.test"}, devNull(t), devNull(t), paths); code != 0 {
		t.Fatalf("cli.DoConnect = %d", code)
	}

	cfg := readConfig(t, paths.ConfigFile)
	if cfg["theme"] == nil {
		t.Error("connect discarded the user's theme; it is not licence to reset the config")
	}
	apis, _ := cfg["apis"].(map[string]any)
	if apis["other"] == nil {
		t.Error("connect discarded another registered API")
	}
	entry, _ := apis[cli.APIName].(map[string]any)
	if entry["base_url"] != "http://new.test" {
		t.Errorf("base_url = %v, want the new URL", entry["base_url"])
	}
	// Credentials and hand-written profiles survive a URL switch, so moving
	// between two hosts serving the same tenant does not force a re-login.
	if entry["profiles"] == nil {
		t.Error("connect discarded the user's profiles; a URL switch must not force a re-login")
	}
}

func TestConnectRejectsInjectionURLs(t *testing.T) {
	for _, bad := range []string{
		`http://evil.com/"$(id)"`,
		"http://evil.com/`id`",
		"ftp://example.com",
		"not-a-url",
		"",
	} {
		paths := testPaths(t)
		code := cli.DoConnect([]string{bad}, devNull(t), devNull(t), paths)
		if code == 0 {
			t.Errorf("cli.DoConnect(%q) = 0, want a non-zero usage failure", bad)
		}
		if _, err := os.Stat(paths.ConfigFile); err == nil {
			t.Errorf("cli.DoConnect(%q) wrote a config despite refusing the URL", bad)
		}
	}
}

func TestConnectUsageErrors(t *testing.T) {
	for _, args := range [][]string{{}, {"a", "b"}, {"  "}} {
		if code := cli.DoConnect(args, devNull(t), devNull(t), testPaths(t)); code != 2 {
			t.Errorf("cli.DoConnect(%v) = %d, want 2 for a usage error", args, code)
		}
	}
}

func TestConnectRefusesToClobberUnparseableConfig(t *testing.T) {
	paths := testPaths(t)
	if err := os.MkdirAll(paths.ConfigDir, 0o700); err != nil {
		t.Fatal(err)
	}
	broken := "{ this is not json"
	if err := os.WriteFile(paths.ConfigFile, []byte(broken), 0o600); err != nil {
		t.Fatal(err)
	}
	if code := cli.DoConnect([]string{"http://new.test"}, devNull(t), devNull(t), paths); code == 0 {
		t.Error("connect should refuse rather than silently discard a config it cannot parse")
	}
	body, _ := os.ReadFile(paths.ConfigFile)
	if string(body) != broken {
		t.Error("connect overwrote a config it could not parse; the user's data must survive")
	}
}

func TestConnectAcceptsJSONCConfig(t *testing.T) {
	// restish writes `//` comments into the config, so a comment-bearing file is
	// a *valid* config, not a broken one. Refusing it would break `connect` for
	// exactly the users who came from restish.
	paths := testPaths(t)
	if err := os.MkdirAll(paths.ConfigDir, 0o700); err != nil {
		t.Fatal(err)
	}
	body := "{\n  // migrated from v1\n  \"apis\": {\"store\": {\"base_url\": \"http://old.test\"}}\n}"
	if err := os.WriteFile(paths.ConfigFile, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if code := cli.DoConnect([]string{"http://new.test"}, devNull(t), devNull(t), paths); code != 0 {
		t.Fatalf("cli.DoConnect on a JSONC config = %d, want 0", code)
	}
	cfg := readConfig(t, paths.ConfigFile)
	apis, _ := cfg["apis"].(map[string]any)
	entry, _ := apis[cli.APIName].(map[string]any)
	if entry["base_url"] != "http://new.test" {
		t.Errorf("base_url = %v, want the new URL", entry["base_url"])
	}
}

// ---------------------------------------------------------------------------
// cli.LocalVerb routing
// ---------------------------------------------------------------------------

func TestLocalVerbOnlyClaimsOurOwnVerbs(t *testing.T) {
	// Generated operations must pass through untouched. A verb list that
	// accidentally swallowed `list-skills` would be a silent, total breakage.
	for _, argv := range [][]string{
		{"sbs"},
		{"sbs", "list-skills"},
		{"sbs", "get-skill", "x"},
		{"sbs", "--help"},
		{"sbs", "--cli.Version"},
		{"sbs", "cli", "doctor"},
		{"sbs", "whoami"},
		{"sbs", "login"},  // handled later, via the engine, not by cli.LocalVerb
		{"sbs", "logout"}, // ditto
	} {
		if handled, _ := cli.LocalVerb(argv, devNull(t), devNull(t), cli.MapEnviron{}, testPaths(t)); handled {
			t.Errorf("cli.LocalVerb claimed %v; it must fall through to restish", argv)
		}
	}
}

func TestLocalVerbClaimsConnect(t *testing.T) {
	paths := testPaths(t)
	handled, code := cli.LocalVerb([]string{"sbs", "connect", "http://x.test"}, devNull(t), devNull(t), cli.MapEnviron{}, paths)
	if !handled {
		t.Fatal("cli.LocalVerb must claim `connect`")
	}
	if code != 0 {
		t.Errorf("connect exit = %d, want 0", code)
	}
}

// ---------------------------------------------------------------------------
// dist flag parsing
// ---------------------------------------------------------------------------

func TestParseDistFlags(t *testing.T) {
	got, err := cli.ParseDistFlags([]string{"--platform", "darwin-arm64", "--output", "/tmp/sbs", "--format", "archive"}, "download-cli")
	if err != nil {
		t.Fatalf("cli.ParseDistFlags: %v", err)
	}
	if got.Platform != "darwin-arm64" || got.Output != "/tmp/sbs" || got.Format != "archive" {
		t.Errorf("parsed = %+v", got)
	}
}

func TestParseDistFlagsAcceptsInlineValues(t *testing.T) {
	// `--flag=value` is what people actually type, and every generated operation
	// on this same CLI accepts it. Rejecting it here would be a gratuitous
	// inconsistency.
	got, err := cli.ParseDistFlags([]string{"--platform=linux-arm64", "--format=raw"}, "download-cli")
	if err != nil {
		t.Fatalf("cli.ParseDistFlags: %v", err)
	}
	if got.Platform != "linux-arm64" || got.Format != "raw" {
		t.Errorf("parsed = %+v", got)
	}
}

func TestParseDistFlagsDefaultsToRaw(t *testing.T) {
	got, err := cli.ParseDistFlags(nil, "download-cli")
	if err != nil {
		t.Fatalf("cli.ParseDistFlags: %v", err)
	}
	if got.Format != "raw" {
		t.Errorf("format = %q, want raw by default (a CLI user wants an executable, not a tarball)", got.Format)
	}
}

func TestParseDistFlagsRejectsBadInput(t *testing.T) {
	for _, args := range [][]string{
		{"--format", "tarball"},
		{"--platform"},
		{"--nonsense"},
		{"positional"},
	} {
		if _, err := cli.ParseDistFlags(args, "download-cli"); err == nil {
			t.Errorf("cli.ParseDistFlags(%v) = nil error, want a failure", args)
		}
	}
}

func TestCurrentPlatformIsGoosGoarch(t *testing.T) {
	// The CLI always sends an explicit ?platform= built from runtime.GOOS and
	// runtime.GOARCH, which is exact — unlike the server's User-Agent sniffing,
	// which cannot distinguish Apple Silicon from Intel.
	want := runtime.GOOS + "-" + runtime.GOARCH
	if got := cli.CurrentPlatform(); got != want {
		t.Errorf("cli.CurrentPlatform() = %q, want %q", got, want)
	}
	if !strings.Contains(want, "-") {
		t.Errorf("platform id %q is not <goos>-<goarch>", want)
	}
}

func TestArchiveSuffixPerPlatform(t *testing.T) {
	for platform, want := range map[string]string{
		"linux-amd64":   ".tar.gz",
		"darwin-arm64":  ".tar.gz",
		"windows-amd64": ".zip",
	} {
		if got := cli.ArchiveSuffix(platform); got != want {
			t.Errorf("cli.ArchiveSuffix(%q) = %q, want %q", platform, got, want)
		}
	}
}
