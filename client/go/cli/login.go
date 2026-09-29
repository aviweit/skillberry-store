package cli

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/rest-sh/restish/v2/auth"
)

// The thin auth verbs of §4.3.
//
// Neither is required by the flow: the auth handler authenticates on demand, so
// `sbs list-skills` on a fresh install prompts and works with no `login` step.
// They exist because users expect them — and because "how do I sign out" needs
// an answer that is not "delete a file".
//
// Both are implemented by dispatching into the engine rather than by touching
// the token cache directly. `sbs cli auth logout` is upstream's own
// "delete cached API auth tokens" command for a promoted API, and it derives
// the cache key the same way the request pipeline does. Deriving that key a
// second time here would be a copy that silently rots the day upstream changes
// the scheme — and the failure would be a token that never gets cleared.

// DoLogin pre-authenticates: drop any cached token, then make one authenticated
// request so the handler prompts and caches a fresh token.
func DoLogin(args []string, stdout, stderr *os.File, env Environ) int {
	if len(args) > 0 {
		WriteLine(stderr, fmt.Sprintf("usage: %s login", CLIName))
		return 2
	}

	base := BaseURL(env)

	// Fail fast when the store has auth switched off, exactly as the shim did
	// (its _do_login returned 2 with this message). Prompting for credentials
	// the server will ignore is worse than useless: the user reasonably
	// concludes their password was wrong.
	if AuthDisabled(base) {
		WriteLine(stderr, fmt.Sprintf("Authentication is disabled on %s; no login required.", base))
		return 2
	}

	// Clear first so `sbs login` means "authenticate now" and not "no-op
	// because a token is already cached". A user typing `login` after their
	// permissions changed expects a fresh token.
	if err := NewCLI(env).Run([]string{CLIName, SupportNamespace, "auth", "logout"}); err != nil {
		// Not fatal: there may simply be no cached token to delete, which is
		// the normal state on a first login.
		WriteLine(stderr, fmt.Sprintf("Note: could not clear the cached token: %v", err))
	}

	// `whoami` is the cheapest authenticated operation the store has, and it
	// prints the resolved identity — which is the "Signed in as …" line the
	// shim printed, produced by the server rather than guessed by us.
	if err := NewCLI(env).Run([]string{CLIName, "whoami"}); err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, WrapRunError(err, base)))
		return 1
	}
	return 0
}

// DoLogout revokes the token server-side, then drops the local copy.
//
// Order matters: revoke while we still have the credential to authenticate the
// revocation, then delete. Doing it the other way round would leave a live token
// on the server that nobody can revoke any more.
func DoLogout(args []string, stdout, stderr *os.File, env Environ) int {
	if len(args) > 0 {
		WriteLine(stderr, fmt.Sprintf("usage: %s logout", CLIName))
		return 2
	}

	// Best effort, and deliberately so: the token may already be expired,
	// revoked or unknown, and the store answers 200 for all of those
	// (auth_api.py documents logout as idempotent). What must not happen is a
	// failed server call leaving the token cached locally — so this error is
	// noted, not returned.
	if err := NewCLI(env).Run([]string{CLIName, "logout"}); err != nil {
		WriteLine(stderr, "Note: could not revoke the token on the server; clearing it locally anyway.")
	}

	if err := NewCLI(env).Run([]string{CLIName, SupportNamespace, "auth", "logout"}); err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: could not clear the cached token: %v", CLIName, err))
		return 1
	}

	fmt.Fprintln(stdout, "Signed out")
	return 0
}

// AuthDisabled reports whether the store is running with access control off,
// via the same GET /auth/whoami probe the auth handler uses.
//
// Standalone rather than reusing StandaloneAuth.Probe because this call has no
// AuthContext to borrow an HTTPClient from: it runs before any engine dispatch.
// A short timeout keeps `login` responsive against a wedged host — the answer
// only decides whether to print one line, so waiting is never worth it.
func AuthDisabled(base string) bool {
	h := &StandaloneAuth{}
	ac := auth.AuthContext{
		BaseURL:    base,
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
		Stderr:     io.Discard,
	}
	return h.Probe(context.Background(), base, ac).AuthDisabled
}
