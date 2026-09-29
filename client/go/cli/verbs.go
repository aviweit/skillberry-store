package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// LocalVerb handles the commands this program owns, before restish parses argv.
//
// Keeping them here rather than registering cobra commands on the embedded CLI
// is deliberate (§G4): upstream's embedding API is documented as a "first pass"
// with no custom-command hook yet, and every one of these verbs needs only a
// config write or an HTTP fetch. Running before Run() means we depend on
// nothing upstream has not promised, so pinning restish stays a cheap decision.
//
// Returns handled=false for anything not ours, which then flows to restish
// untouched — including every generated operation.
func LocalVerb(argv []string, stdout, stderr *os.File, env Environ, paths BrandedPaths) (handled bool, code int) {
	if len(argv) < 2 {
		return false, 0
	}

	switch argv[1] {
	case "connect":
		return true, DoConnect(argv[2:], stdout, stderr, paths)
	case "download-cli":
		return true, DoDownloadCLI(argv[2:], stdout, stderr, env)
	case "self-update":
		return true, DoSelfUpdate(argv[2:], stdout, stderr, env)
	}
	return false, 0
}

// DoConnect repoints the CLI at a different store by writing the base and spec
// URLs into the user's own config, where they override the baked defaults.
//
// This is ~20 lines against the shim's `_do_connect` + `_restish_connect` +
// `_ensure_env_profile` + `_registered_base` (~70 lines of subprocess parsing),
// because there is no first-run registration step to perform any more: the
// baked default config already registers the API, so `connect` only has to
// record a *deviation* from it.
//
// Credentials are deliberately preserved (the shim made the same choice): a URL
// switch between two hosts serving the same tenant should not force a re-login.
// Tokens live in restish's token cache keyed per API, not in this file, so
// leaving the file's other keys alone is all that takes.
func DoConnect(args []string, stdout, stderr *os.File, paths BrandedPaths) int {
	if len(args) != 1 || strings.TrimSpace(args[0]) == "" {
		WriteLine(stderr, fmt.Sprintf("usage: %s connect <url>", CLIName))
		return 2
	}
	url := strings.TrimRight(strings.TrimSpace(args[0]), "/")

	// The same validator that gates -ldflags and the generated install scripts
	// (§5.7). Here it is not an injection guard — nothing is exec'd — but a
	// typo guard: a bad URL written to config fails on every later command with
	// a spec-discovery error, far from the place that caused it.
	if err := ValidateURLForConnect(url); err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 2
	}

	if paths.ConfigFile == "" {
		WriteLine(stderr, fmt.Sprintf("%s: cannot determine where to write configuration", CLIName))
		return 1
	}

	// Read-modify-write rather than truncate: the file holds the user's theme,
	// their other APIs and any profile they added by hand. `connect` is not
	// licence to discard those.
	cfg := map[string]any{}
	if raw, err := os.ReadFile(paths.ConfigFile); err == nil {
		if err := json.Unmarshal([]byte(StripJSONC(string(raw))), &cfg); err != nil {
			WriteLine(stderr, fmt.Sprintf("%s: %s is not valid JSON; refusing to overwrite it (%v)",
				CLIName, paths.ConfigFile, err))
			return 1
		}
	}

	apis, _ := cfg["apis"].(map[string]any)
	if apis == nil {
		apis = map[string]any{}
	}
	entry, _ := apis[APIName].(map[string]any)
	if entry == nil {
		entry = map[string]any{}
	}
	entry["base_url"] = url
	entry["spec_url"] = url + "/openapi.json"
	apis[APIName] = entry
	cfg["apis"] = apis

	if err := WriteConfigAtomic(paths, cfg); err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: could not write %s: %v", CLIName, paths.ConfigFile, err))
		return 1
	}

	fmt.Fprintf(stdout, "Connected to %s\n", url)
	return 0
}

// ValidateURLForConnect applies the shared URL grammar but not the slot-width
// limit: a URL the user types into their own config never has to fit in a
// patchable slot, so rejecting a long-but-valid URL here would be a rule
// borrowed from an unrelated mechanism.
func ValidateURLForConnect(raw string) error {
	if raw == "" {
		return fmt.Errorf("URL is empty")
	}
	if !URLPattern.MatchString(raw) {
		return fmt.Errorf("URL %q is not an acceptable http(s) URL", raw)
	}
	return nil
}

// WriteConfigAtomic writes the config through a temp file in the same directory
// and renames it into place at 0600.
//
// Atomic because this file can carry a bearer token and is read on every single
// command: a torn write leaves the CLI unable to start until the user finds and
// deletes it. 0600 because of the same token — the Python shim chmod'd its
// config for exactly this reason.
func WriteConfigAtomic(paths BrandedPaths, cfg map[string]any) error {
	if err := os.MkdirAll(paths.ConfigDir, 0o700); err != nil {
		return err
	}
	body, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	body = append(body, '\n')

	tmp, err := os.CreateTemp(paths.ConfigDir, filepath.Base(paths.ConfigFile)+".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename

	if _, err := tmp.Write(body); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		return err
	}
	return os.Rename(tmpName, paths.ConfigFile)
}
