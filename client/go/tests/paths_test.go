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

// §8.1 #1 — cli.BrandPaths sets the three env vars, respects a user-set value, and
// produces branded paths.

func TestBrandPathsSetsBrandedPaths(t *testing.T) {
	env := cli.MapEnviron{}
	got, err := cli.BrandPaths(env, "/home/alice")
	if err != nil {
		t.Fatalf("cli.BrandPaths: %v", err)
	}

	wantDir := filepath.Join("/home/alice", ".config", "sbs")
	wantFile := filepath.Join(wantDir, "sbs.json")
	wantCache := filepath.Join("/home/alice", ".cache", "sbs")

	if got.ConfigDir != wantDir || got.ConfigFile != wantFile || got.CacheDir != wantCache {
		t.Fatalf("paths = %+v, want dir=%s file=%s cache=%s", got, wantDir, wantFile, wantCache)
	}
	// The env vars are what restish actually reads; asserting only the returned
	// struct would pass even if we forgot to export them.
	for key, want := range map[string]string{
		"RSH_CONFIG_DIR": wantDir,
		"RSH_CONFIG":     wantFile,
		"RSH_CACHE_DIR":  wantCache,
	} {
		if env[key] != want {
			t.Errorf("%s = %q, want %q", key, env[key], want)
		}
	}
	// The whole point of §3.3's last row: the config *filename* must be branded,
	// or `doctor` and `config path` keep printing "restish.json".
	if filepath.Base(got.ConfigFile) != "sbs.json" {
		t.Errorf("config filename = %q, want sbs.json", filepath.Base(got.ConfigFile))
	}
}

func TestBrandPathsRespectsUserSetValues(t *testing.T) {
	env := cli.MapEnviron{
		"RSH_CONFIG_DIR": "/custom/cfg",
		"RSH_CACHE_DIR":  "/custom/cache",
	}
	got, err := cli.BrandPaths(env, "/home/alice")
	if err != nil {
		t.Fatalf("cli.BrandPaths: %v", err)
	}
	if got.ConfigDir != "/custom/cfg" {
		t.Errorf("ConfigDir = %q, want the user's /custom/cfg", got.ConfigDir)
	}
	if got.CacheDir != "/custom/cache" {
		t.Errorf("CacheDir = %q, want the user's /custom/cache", got.CacheDir)
	}
	// RSH_CONFIG was NOT set by the user, so it is derived — and it must be
	// derived from the directory the user chose, not from ~/.config. Landing
	// the config file outside the directory they pointed us at would be a
	// confusing split-brain.
	if want := filepath.Join("/custom/cfg", "sbs.json"); got.ConfigFile != want {
		t.Errorf("ConfigFile = %q, want %q", got.ConfigFile, want)
	}
}

func TestBrandPathsRespectsUserSetConfigFile(t *testing.T) {
	env := cli.MapEnviron{"RSH_CONFIG": "/ci/pinned.json"}
	got, err := cli.BrandPaths(env, "/home/alice")
	if err != nil {
		t.Fatalf("cli.BrandPaths: %v", err)
	}
	if got.ConfigFile != "/ci/pinned.json" {
		t.Errorf("ConfigFile = %q, want the user's /ci/pinned.json", got.ConfigFile)
	}
}

func TestBrandPathsHonoursXDG(t *testing.T) {
	env := cli.MapEnviron{"XDG_CONFIG_HOME": "/xdg/cfg", "XDG_CACHE_HOME": "/xdg/cache"}
	got, err := cli.BrandPaths(env, "/home/alice")
	if err != nil {
		t.Fatalf("cli.BrandPaths: %v", err)
	}
	// restish honours XDG too, so ignoring it would put our config somewhere
	// restish would not look for it.
	if want := filepath.Join("/xdg/cfg", "sbs"); got.ConfigDir != want {
		t.Errorf("ConfigDir = %q, want %q", got.ConfigDir, want)
	}
	if want := filepath.Join("/xdg/cache", "sbs"); got.CacheDir != want {
		t.Errorf("CacheDir = %q, want %q", got.CacheDir, want)
	}
}

func TestBrandPathsWithoutHomeFails(t *testing.T) {
	if _, err := cli.BrandPaths(cli.MapEnviron{}, ""); err == nil {
		t.Fatal("cli.BrandPaths with no home should fail rather than build /.config/sbs")
	}
}

// ---------------------------------------------------------------------------
// cli.EnsureConfigFile — the fix without which the whole branded surface reverts
// to stock restish on a fresh install. See the doc comment on cli.EnsureConfigFile.
// ---------------------------------------------------------------------------

func TestEnsureConfigFileCreatesPrivateEmptyConfig(t *testing.T) {
	dir := t.TempDir()
	paths := cli.BrandedPaths{
		ConfigDir:  filepath.Join(dir, ".config", "sbs"),
		ConfigFile: filepath.Join(dir, ".config", "sbs", "sbs.json"),
	}
	if err := cli.EnsureConfigFile(paths); err != nil {
		t.Fatalf("cli.EnsureConfigFile: %v", err)
	}

	body, err := os.ReadFile(paths.ConfigFile)
	if err != nil {
		t.Fatalf("config file not created: %v", err)
	}
	// Must be valid JSON: restish parses it eagerly and a parse failure is the
	// very thing this function exists to prevent.
	var parsed map[string]any
	if err := json.Unmarshal(body, &parsed); err != nil {
		t.Fatalf("bootstrap config is not valid JSON (%q): %v", body, err)
	}
	if len(parsed) != 0 {
		t.Errorf("bootstrap config should be empty so baked defaults apply, got %v", parsed)
	}

	if runtime.GOOS != "windows" {
		// restish refuses to read a group/world-readable config, so anything
		// looser than 0600 makes every command fail with a permissions error.
		info, err := os.Stat(paths.ConfigFile)
		if err != nil {
			t.Fatal(err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Errorf("config mode = %o, want 600", perm)
		}
	}
}

func TestEnsureConfigFileLeavesExistingUntouched(t *testing.T) {
	dir := t.TempDir()
	paths := cli.BrandedPaths{ConfigDir: dir, ConfigFile: filepath.Join(dir, "sbs.json")}
	original := `{"apis":{"store":{"base_url":"http://example.test"}}}`
	if err := os.WriteFile(paths.ConfigFile, []byte(original), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := cli.EnsureConfigFile(paths); err != nil {
		t.Fatalf("cli.EnsureConfigFile: %v", err)
	}
	body, _ := os.ReadFile(paths.ConfigFile)
	if string(body) != original {
		t.Errorf("existing config was modified: %q", body)
	}
}

// ---------------------------------------------------------------------------
// §8.1 #3 — BaseURL precedence
// ---------------------------------------------------------------------------

func TestBaseURLPrefersEnvOverSlot(t *testing.T) {
	got := cli.BaseURL(cli.MapEnviron{cli.URLEnvVar: "http://from-env.test:9000/"})
	if got != "http://from-env.test:9000" {
		t.Errorf("cli.BaseURL = %q, want the env value with the trailing slash stripped", got)
	}
}

func TestBaseURLFallsBackToSlot(t *testing.T) {
	got := cli.BaseURL(cli.MapEnviron{})
	if got != cli.SlotValue() {
		t.Errorf("cli.BaseURL = %q, want the slot value %q", got, cli.SlotValue())
	}
	if strings.Contains(got, string(cli.SlotPad)) {
		t.Errorf("cli.BaseURL = %q still carries slot padding", got)
	}
}

func TestBaseURLIgnoresBlankEnv(t *testing.T) {
	// An exported-but-empty SBS_URL is a very common shell accident
	// (`export SBS_URL=$SOME_UNSET_VAR`). Treating it as a real override would
	// point the CLI at "" and fail obscurely.
	if got := cli.BaseURL(cli.MapEnviron{cli.URLEnvVar: "   "}); got != cli.SlotValue() {
		t.Errorf("cli.BaseURL = %q, want the slot value for a blank env var", got)
	}
}

// TestURLSlotIsPatchable guards the §3.4 #1 / G8 trap: `-ldflags -X` silently
// no-ops unless the target is a package-level string with a constant
// initializer, and the slot must stay exactly cli.SlotWidth bytes wide so the
// `patch` mechanism can rewrite it without changing the file size.
func TestURLSlotIsPatchable(t *testing.T) {
	if len(cli.URLSlot) != cli.SlotWidth {
		t.Errorf("len(cli.URLSlot) = %d, want exactly cli.SlotWidth (%d); "+
			"the patch mechanism locates and rewrites a fixed-width slot",
			len(cli.URLSlot), cli.SlotWidth)
	}
	if !strings.HasSuffix(cli.URLSlot, string(cli.SlotPad)) {
		t.Errorf("cli.URLSlot = %q has no %q padding, so a shorter URL could not be patched in",
			cli.URLSlot, string(cli.SlotPad))
	}
	if trimmed := cli.SlotValue(); trimmed == "" {
		t.Error("cli.SlotValue() is empty; the compiled-in default must be usable")
	}
}

// ---------------------------------------------------------------------------
// URL validation (§5.7, §7.2, B14)
// ---------------------------------------------------------------------------

func TestValidateURLRejectsInjection(t *testing.T) {
	// Every one of these is a real injection or a real footgun, not a
	// hypothetical: the first is the design's own example of what must never
	// reach a generated shell script.
	bad := []string{
		`http://evil.com/"$(id)"`,
		"http://evil.com/`id`",
		"http://evil.com/;id",
		"http://evil.com/$(id)",
		"http://evil.com/|id",
		"http://evil.com/\nid",
		"http://evil.com/ id",
		"ftp://example.com",
		"file:///etc/passwd",
		"javascript:alert(1)",
		"example.com",
		"",
		// Longer than the slot: accepting it would either truncate the URL or
		// overrun the slot and corrupt the binary.
		"http://" + strings.Repeat("a", 80) + ".com",
	}
	for _, u := range bad {
		if err := cli.ValidateURL(u); err == nil {
			t.Errorf("cli.ValidateURL(%q) = nil, want an error", u)
		}
	}
}

func TestValidateURLAcceptsRealURLs(t *testing.T) {
	good := []string{
		"http://localhost:8000",
		"https://store.example.com",
		"https://store.example.com/prefix",
		"https://store.example.com:8443/a/b",
		"http://127.0.0.1:8099",
		"https://my-store.internal",
	}
	for _, u := range good {
		if err := cli.ValidateURL(u); err != nil {
			t.Errorf("cli.ValidateURL(%q) = %v, want nil", u, err)
		}
	}
}

func TestValidateURLForConnectAllowsLongURLs(t *testing.T) {
	// A URL the user types into their own config never has to fit a patchable
	// slot, so the width limit must not leak into `connect`.
	long := "https://" + strings.Repeat("a", 80) + ".example.com/store"
	if err := cli.ValidateURLForConnect(long); err != nil {
		t.Errorf("cli.ValidateURLForConnect(long) = %v, want nil", err)
	}
	if err := cli.ValidateURL(long); err == nil {
		t.Error("cli.ValidateURL(long) should still enforce the slot width")
	}
}

// ---------------------------------------------------------------------------
// §8.1 #2 — config migration copies only apis.sbs, sets 0600, leaves the old
// file, prints one line.
// ---------------------------------------------------------------------------

func TestMigrateLegacyConfigCopiesOnlySBSEntry(t *testing.T) {
	dir := t.TempDir()
	legacy := filepath.Join(dir, "restish.json")
	// Deliberately includes a `//` comment (restish writes a migration header,
	// which is why plain json.Unmarshal is not enough), a URL containing "//"
	// that must survive comment stripping, another API, and a theme.
	legacyBody := `{
  // restish v1 -> v2 migration
  "apis": {
    "sbs": {"base_url": "http://store.test:8000", "spec_url": "http://store.test:8000/openapi.json"},
    "other": {"base_url": "http://other.test"}
  },
  "theme": {"keyword": "#ff0000"}
}`
	if err := os.WriteFile(legacy, []byte(legacyBody), 0o600); err != nil {
		t.Fatal(err)
	}

	paths := cli.BrandedPaths{
		ConfigDir:  filepath.Join(dir, "sbs"),
		ConfigFile: filepath.Join(dir, "sbs", "sbs.json"),
	}
	notice, err := cli.MigrateLegacyConfig(paths, legacy)
	if err != nil {
		t.Fatalf("cli.MigrateLegacyConfig: %v", err)
	}
	if notice == "" {
		t.Error("migration should return exactly one notice line for stderr")
	}
	if strings.Count(notice, "\n") != 0 {
		t.Errorf("notice should be one line, got %q", notice)
	}

	body, err := os.ReadFile(paths.ConfigFile)
	if err != nil {
		t.Fatalf("migrated config not written: %v", err)
	}
	var got struct {
		APIs map[string]struct {
			BaseURL string `json:"base_url"`
		} `json:"apis"`
		Theme map[string]string `json:"theme"`
	}
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("migrated config is not valid JSON: %v", err)
	}

	if got.APIs[cli.CLIName].BaseURL != "http://store.test:8000" {
		t.Errorf("apis.%s.base_url = %q, want the legacy value (the '//' in the URL must survive comment stripping)",
			cli.CLIName, got.APIs[cli.CLIName].BaseURL)
	}
	// Only apis.sbs: inheriting the user's other APIs or their theme would
	// silently import configuration they never gave this CLI.
	if _, ok := got.APIs["other"]; ok {
		t.Error("migration copied apis.other; it must copy only apis." + cli.CLIName)
	}
	if len(got.Theme) != 0 {
		t.Error("migration copied the theme; it must copy only apis." + cli.CLIName)
	}

	if runtime.GOOS != "windows" {
		info, err := os.Stat(paths.ConfigFile)
		if err != nil {
			t.Fatal(err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Errorf("migrated config mode = %o, want 600 (it can carry a bearer token)", perm)
		}
	}

	// The old file is a copy source, not a move source: a user who also drives
	// restish directly must be unaffected.
	if _, err := os.Stat(legacy); err != nil {
		t.Errorf("legacy config was removed or altered: %v", err)
	}
}

func TestMigrateLegacyConfigNeverOverwrites(t *testing.T) {
	dir := t.TempDir()
	legacy := filepath.Join(dir, "restish.json")
	if err := os.WriteFile(legacy, []byte(`{"apis":{"sbs":{"base_url":"http://old.test"}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	paths := cli.BrandedPaths{ConfigDir: dir, ConfigFile: filepath.Join(dir, "sbs.json")}
	current := `{"apis":{"store":{"base_url":"http://current.test"}}}`
	if err := os.WriteFile(paths.ConfigFile, []byte(current), 0o600); err != nil {
		t.Fatal(err)
	}

	notice, err := cli.MigrateLegacyConfig(paths, legacy)
	if err != nil {
		t.Fatalf("cli.MigrateLegacyConfig: %v", err)
	}
	if notice != "" {
		t.Errorf("notice = %q, want none when a config already exists", notice)
	}
	body, _ := os.ReadFile(paths.ConfigFile)
	if string(body) != current {
		t.Errorf("existing config was overwritten: %q", body)
	}
}

func TestMigrateLegacyConfigNoLegacyFileIsSilent(t *testing.T) {
	dir := t.TempDir()
	paths := cli.BrandedPaths{ConfigDir: dir, ConfigFile: filepath.Join(dir, "sbs.json")}
	notice, err := cli.MigrateLegacyConfig(paths, filepath.Join(dir, "does-not-exist.json"))
	if err != nil {
		t.Fatalf("cli.MigrateLegacyConfig: %v", err)
	}
	if notice != "" {
		t.Errorf("notice = %q, want silence on the common path", notice)
	}
	if _, err := os.Stat(paths.ConfigFile); !os.IsNotExist(err) {
		t.Error("migration created a config file when there was nothing to migrate")
	}
}

func TestMigrateLegacyConfigWithoutSBSEntryIsSilent(t *testing.T) {
	dir := t.TempDir()
	legacy := filepath.Join(dir, "restish.json")
	// A user who runs restish for other APIs but never used our shim.
	if err := os.WriteFile(legacy, []byte(`{"apis":{"other":{"base_url":"http://other.test"}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	paths := cli.BrandedPaths{ConfigDir: dir, ConfigFile: filepath.Join(dir, "sbs.json")}
	notice, err := cli.MigrateLegacyConfig(paths, legacy)
	if err != nil {
		t.Fatalf("cli.MigrateLegacyConfig: %v", err)
	}
	if notice != "" {
		t.Errorf("notice = %q, want silence when there is no apis.%s entry", notice, cli.CLIName)
	}
}

func TestStripJSONCKeepsURLsInStrings(t *testing.T) {
	// The bug this guards: a naive comment stripper eats "//store.test" out of
	// "http://store.test" and turns a valid config into a parse error.
	in := `{"url": "http://store.test/x", /* block */ "a": 1 // line
}`
	out := cli.StripJSONC(in)
	if !strings.Contains(out, "http://store.test/x") {
		t.Errorf("cli.StripJSONC ate a URL inside a string: %q", out)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("cli.StripJSONC output is not valid JSON (%q): %v", out, err)
	}
	if parsed["url"] != "http://store.test/x" {
		t.Errorf("url = %v, want the original", parsed["url"])
	}
}

func TestLegacyConfigPathUsesRestishDir(t *testing.T) {
	got := cli.LegacyConfigPath(cli.MapEnviron{}, "/home/alice")
	want := filepath.Join("/home/alice", ".config", "restish", "restish.json")
	if got != want {
		t.Errorf("cli.LegacyConfigPath = %q, want %q", got, want)
	}
	// A user with XDG set had restish put its config under XDG too, so looking
	// in ~/.config would miss the very config we are migrating.
	gotXDG := cli.LegacyConfigPath(cli.MapEnviron{"XDG_CONFIG_HOME": "/xdg"}, "/home/alice")
	if wantXDG := filepath.Join("/xdg", "restish", "restish.json"); gotXDG != wantXDG {
		t.Errorf("cli.LegacyConfigPath with XDG = %q, want %q", gotXDG, wantXDG)
	}
}
