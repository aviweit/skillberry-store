package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// CLIName is the one name the user ever sees. It is also the config/cache
// directory name and the config file's basename, which is what removes the
// last "restish" string from `doctor` and `config path` (§3.3, D3).
const CLIName = "sbs"

// URLEnvVar / TokenEnvVar are the client-side knobs from §6.
const (
	URLEnvVar   = "SBS_URL"
	TokenEnvVar = "SBS_TOKEN"
)

// BrandedPaths is where BrandPaths() decided the three RSH_* variables should
// point. Returned so tests can assert on it without reading the environment
// back, and so `doctor`-adjacent code has one source of truth.
type BrandedPaths struct {
	ConfigDir  string
	ConfigFile string
	CacheDir   string
}

// BrandPaths points restish's config, config-file and cache locations at
// ~/.config/sbs, ~/.config/sbs/sbs.json and ~/.cache/sbs (§4.4).
//
// This is what makes `sbs cli doctor` and `sbs cli config path` print branded
// paths: restish reads these three env vars before anything else
// (config/paths.go), so setting them here is the whole of it — no upstream
// patch and no output rewriting.
//
// A value the user already set is never overridden. Someone who exports
// RSH_CONFIG_DIR to share one config with restish proper, or who points
// RSH_CONFIG at a checked-in file for CI, has made a deliberate choice; taking
// it away would be a silent config switch, which is far worse than an
// unbranded path in one help line.
//
// Returns the effective paths, including any the user supplied, so the caller
// reports what is actually in force rather than what we would have chosen.
func BrandPaths(env Environ, homeDir string) (BrandedPaths, error) {
	if homeDir == "" {
		// No HOME at all (a scratch container, a daemon). restish has its own
		// fallbacks and its own error message for this; forcing a path built
		// from an empty string would produce "/.config/sbs", which is worse
		// than letting upstream explain itself.
		return BrandedPaths{}, fmt.Errorf("cannot determine home directory")
	}

	configDir := filepath.Join(homeDir, ".config", CLIName)
	cacheDir := filepath.Join(homeDir, ".cache", CLIName)

	// XDG wins over ~/.config when the user has set it: that is the whole
	// point of the variable, and restish honours it too, so ignoring it here
	// would put our config somewhere restish would not look.
	if xdg := env.Get("XDG_CONFIG_HOME"); xdg != "" {
		configDir = filepath.Join(xdg, CLIName)
	}
	if xdg := env.Get("XDG_CACHE_HOME"); xdg != "" {
		cacheDir = filepath.Join(xdg, CLIName)
	}

	if v := env.Get("RSH_CONFIG_DIR"); v != "" {
		configDir = v
	} else {
		env.Set("RSH_CONFIG_DIR", configDir)
	}

	if v := env.Get("RSH_CACHE_DIR"); v != "" {
		cacheDir = v
	} else {
		env.Set("RSH_CACHE_DIR", cacheDir)
	}

	// RSH_CONFIG is the *file*. Derived from the effective config dir rather
	// than recomputed, so a user-set RSH_CONFIG_DIR still yields
	// <their dir>/sbs.json instead of a file outside the directory they chose.
	configFile := filepath.Join(configDir, CLIName+".json")
	if v := env.Get("RSH_CONFIG"); v != "" {
		configFile = v
	} else {
		env.Set("RSH_CONFIG", configFile)
	}

	return BrandedPaths{ConfigDir: configDir, ConfigFile: configFile, CacheDir: cacheDir}, nil
}

// EnsureConfigFile creates an empty JSON config when none exists.
//
// This is not housekeeping — without it the CLI does not work at all on a fresh
// install, and it fails in a way that is actively misleading. Setting RSH_CONFIG
// is what brands the config *filename* (§3.3: `sbs.json`, not `restish.json`),
// but restish treats an explicitly-configured config path as a promise that the
// file is there, and v2 deliberately does not fall back to a default when it is
// missing:
//
//	config: --rsh-config ~/.config/sbs/sbs.json does not exist; v2 does not
//	fall back to the default config; create the file or remove the flag
//
// That error makes the whole config load fail, so `cfg.APIs` comes back empty,
// so the promoted API is "not configured", so the command surface is never
// applied — and the binary falls back to the *entire stock restish surface*:
// generic `get`/`post` verbs, `api connect` examples, `plugin`, and the
// "Manage local Restish configuration" / "Print the Restish version" strings
// that §3.3 counts as fixed. Every branding guarantee in Part A quietly
// evaporates, on a fresh install, with a zero exit status.
//
// Writing a `{}` file instead costs nothing: our baked defaults merge underneath
// the user's config, so an empty config means "no overrides", which is exactly
// right for a first run. 0600 because restish refuses to read a
// group/world-readable config (it may hold credentials) — and it is right to.
//
// Failures are returned but not fatal at the call site: a read-only HOME should
// degrade to a clear config error from restish, not a panic from us.
func EnsureConfigFile(paths BrandedPaths) error {
	if paths.ConfigFile == "" {
		return nil
	}
	if _, err := os.Stat(paths.ConfigFile); err == nil {
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(paths.ConfigFile), 0o700); err != nil {
		return err
	}
	// O_EXCL so two concurrent first runs cannot have one truncate the other's
	// freshly written config.
	f, err := os.OpenFile(paths.ConfigFile, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if os.IsExist(err) {
			return nil // another process won the race; its file is just as good
		}
		return err
	}
	defer f.Close()
	_, err = f.WriteString("{}\n")
	return err
}

// Environ abstracts the process environment so BrandPaths is testable without
// mutating the real one (Go's t.Setenv serialises tests and cannot express
// "the user set this but not that" as cleanly).
type Environ interface {
	Get(key string) string
	Set(key, value string) error
}

// OSEnviron is the production Environ: the actual process environment.
type OSEnviron struct{}

func (OSEnviron) Get(key string) string       { return os.Getenv(key) }
func (OSEnviron) Set(key, value string) error { return os.Setenv(key, value) }

// MapEnviron is an in-memory Environ for tests.
type MapEnviron map[string]string

func (m MapEnviron) Get(key string) string { return m[key] }
func (m MapEnviron) Set(key, value string) error {
	m[key] = value
	return nil
}

// ---------------------------------------------------------------------------
// Base URL resolution
// ---------------------------------------------------------------------------

// BaseURL resolves the store URL the artifact should talk to, in the order
// documented in §4.1:
//
//	SBS_URL env  >  the patched slot / -ldflags value  >  the compiled default
//
// User config (`sbs connect`) is deliberately absent from this list. It sits
// *above* everything here, and it gets there for free: SetDefaultConfig merges
// underneath the user's config file, so restish resolves that precedence
// itself. Re-implementing it would mean owning a merge we do not need to own.
func BaseURL(env Environ) string {
	if v := strings.TrimSpace(env.Get(URLEnvVar)); v != "" {
		return strings.TrimRight(v, "/")
	}
	return SlotValue()
}

// SlotValue trims the '#' padding off URLSlot and normalises the trailing
// slash. A slot that was never patched still reads as its source default, and
// a slot patched with a shorter URL reads as exactly that URL.
func SlotValue() string {
	v := strings.TrimRight(URLSlot, string(SlotPad))
	v = strings.TrimSpace(v)
	return strings.TrimRight(v, "/")
}

// URLPattern is the §5.7 validator, shared by every place a URL can reach a
// shell, a linker flag or a patched artifact. Kept identical to the Python
// side (services/cli_artifacts.py) — a validator that drifts between the two
// is the same as no validator, since each guards a different injection point.
var URLPattern = regexp.MustCompile(`^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$`)

// ValidateURL rejects anything that could turn into command or argument
// injection downstream. A value like `evil.com/"$(id)"` is code execution on
// the user's machine once it lands in a generated shell script (§7.2, B14).
func ValidateURL(raw string) error {
	if raw == "" {
		return fmt.Errorf("URL is empty")
	}
	if len(raw) > SlotWidth {
		return fmt.Errorf("URL is %d bytes, which exceeds the %d-byte slot", len(raw), SlotWidth)
	}
	if !URLPattern.MatchString(raw) {
		return fmt.Errorf("URL %q is not an acceptable http(s) URL", raw)
	}
	return nil
}

// ---------------------------------------------------------------------------
// One-time config migration from ~/.config/restish
// ---------------------------------------------------------------------------

// LegacyConfigPath is where a user who drove `sbs` through the Python shim has
// their registration today: the shim called `restish api connect sbs …`, so the
// entry lives under restish's own config as `apis.sbs`.
func LegacyConfigPath(env Environ, homeDir string) string {
	if xdg := env.Get("XDG_CONFIG_HOME"); xdg != "" {
		return filepath.Join(xdg, "restish", "restish.json")
	}
	return filepath.Join(homeDir, ".config", "restish", "restish.json")
}

// MigrateLegacyConfig copies *only* the `apis.sbs` entry out of restish's
// config into ours, once, and returns a one-line notice for stderr (§4.4, G9).
//
// Three properties are deliberate:
//
//   - It runs only when our config file does not exist yet. A user who has
//     configured the new CLI is never second-guessed by an old file.
//   - It copies one key, not the file. Someone who also uses restish directly
//     has themes, other APIs and other profiles in there; inheriting those
//     would silently import configuration they never asked this CLI to have.
//   - The old file is left untouched. Migration is a copy, so the same user's
//     `restish` keeps working exactly as before.
//
// Returns ("", nil) when there is nothing to do, which is the common case.
func MigrateLegacyConfig(paths BrandedPaths, legacyPath string) (string, error) {
	if _, err := os.Stat(paths.ConfigFile); err == nil {
		return "", nil // already configured; never overwrite
	} else if !os.IsNotExist(err) {
		return "", err
	}

	raw, err := os.ReadFile(legacyPath)
	if err != nil {
		return "", nil // no legacy config: the overwhelmingly common path
	}

	var legacy struct {
		APIs map[string]json.RawMessage `json:"apis"`
	}
	// restish writes JSONC (it adds a `//` migration header), so plain
	// json.Unmarshal would fail on a perfectly valid config file. Strip
	// comments first — the same problem the shim's _strip_jsonc solved.
	if err := json.Unmarshal([]byte(StripJSONC(string(raw))), &legacy); err != nil {
		return "", nil // unreadable legacy config is not our problem to report
	}

	entry, ok := legacy.APIs[CLIName]
	if !ok {
		return "", nil
	}

	out := map[string]any{"apis": map[string]json.RawMessage{CLIName: entry}}
	body, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		return "", err
	}

	if err := os.MkdirAll(paths.ConfigDir, 0o700); err != nil {
		return "", err
	}
	// 0600: the copied entry can carry a bearer token. Written via a temp file
	// in the same directory so a crash cannot leave a half-written config that
	// restish would then refuse to parse.
	tmp, err := os.CreateTemp(paths.ConfigDir, ".sbs-config-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op once the rename below succeeds
	if _, err := tmp.Write(body); err != nil {
		tmp.Close()
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		return "", err
	}
	if err := os.Rename(tmpName, paths.ConfigFile); err != nil {
		return "", err
	}

	return fmt.Sprintf("Migrated your %s configuration from %s to %s (the original was left in place).",
		CLIName, legacyPath, paths.ConfigFile), nil
}

// jsoncToken matches, in one alternation, a JSON string literal *or* a comment.
// Order matters: the string alternative comes first so a `//` inside a URL
// ("http://example.com") is seen as part of the string and survives. This is a
// direct port of the shim's _strip_jsonc, which existed for exactly that bug.
var jsoncToken = regexp.MustCompile(`"(?:[^"\\]|\\.)*"|/\*[\s\S]*?\*/|//[^\n]*`)

func StripJSONC(text string) string {
	return jsoncToken.ReplaceAllStringFunc(text, func(m string) string {
		if strings.HasPrefix(m, `"`) {
			return m
		}
		return ""
	})
}

// WriteLine emits one line to stderr, ignoring write errors: a failed notice
// must never turn into a failed command.
func WriteLine(w io.Writer, msg string) {
	fmt.Fprintln(w, msg)
}
