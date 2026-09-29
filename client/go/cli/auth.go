package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/rest-sh/restish/v2/auth"
)

// AuthSchemeName is the auth `type` our baked default config selects. It is
// registered with AddAuthHandler, so restish resolves it the same way it
// resolves its own built-in schemes.
const AuthSchemeName = "sbs-standalone"

// StandaloneAuth authenticates against the store's own /auth/login endpoint
// (docs/design/access-control.md §10.1) as a restish auth.Handler.
//
// This replaces the Python shim's intercepted `login` command, and the change
// in shape is the point: the shim could only authenticate when the user typed
// `sbs login`, because it had already `execvp`'d itself away by the time
// restish saw a 401. A handler is called by the request pipeline, so *any*
// command authenticates on demand — `sbs list-skills` on a fresh install just
// works (§4.3).
//
// Three behaviours are inherited from the shim and must not regress; they are
// the contract docs/design/login-info.md §7 describes, and the reason
// test_sdk_cli_login_info.py could be deleted rather than simply dropped:
//
//  1. `503 auth_disabled` means attach nothing and proceed. The store is in
//     mode: disabled; prompting would ask for credentials it will ignore.
//  2. A `401` may carry the operator's `login_info`. It is printed to stderr
//     ONCE, before the first prompt, and never after a failed attempt — which
//     is what makes the CLI match the UI, where the banner is pre-attempt only.
//  3. Credentials never touch argv. The shim fed restish through stdin to
//     achieve this; here they never leave the process at all.
//
// # Why this never prompts proactively
//
// Authenticate runs before *every* request, including the spec fetch a promoted
// API performs to build its command tree. So it attaches a cached token when it
// has one and otherwise attaches *nothing*, letting the request go out
// unauthenticated. Restish then calls it again with ac.Force set if the response
// was a 401 — which is what SupportsForce below opts into — and only that Force
// pass prompts.
//
// Prompting whenever no token was cached would instead mean:
//
//   - `sbs --help` on a fresh install asks for a password purely to fetch
//     /openapi.json, an endpoint that is in the store's unauthenticated
//     allow-list and needs no credentials at all;
//   - against an unreachable store, a probe cannot tell "needs auth" from
//     "cannot connect", so the user is asked for credentials that a connection
//     error then throws away.
//
// As written: public endpoints never prompt, protected ones prompt exactly once
// at the moment authentication is genuinely required, and an unreachable host
// reports a connection error rather than asking for a password.
type StandaloneAuth struct {
	// Now is injected so token-expiry logic is testable without sleeping.
	// Exported because the test suite lives in a separate package (client/go/tests).
	Now func() time.Time

	// loginInfoOnce guards the "print the operator's message exactly once"
	// rule. It is per-handler rather than per-process because the handler *is*
	// per-process — but a retry after a 401 calls Authenticate again with
	// Force set, and that second call must not reprint the banner.
	loginInfoOnce sync.Once
}

// Parameters declares the config knobs this scheme accepts.
//
// `username` is offered so a user can pin it in config and only be asked for a
// password. No `password` parameter exists, deliberately: a password in
// restish.json is a password in a plaintext file, and the TokenStore already
// gives us somewhere better to put the *result* of authenticating.
func (s *StandaloneAuth) Parameters() []auth.Param {
	return []auth.Param{
		{
			Name:        "username",
			Description: fmt.Sprintf("Username for %s; prompted for when unset", CLIName),
		},
	}
}

// LoginResponse is the shape of POST /auth/login's 200 body (auth_api.py's
// LoginResponse: token, expires_at, tenant_id).
//
// ExpiresAt is an ISO-8601 UTC *instant*, not a duration — so it is stored as a
// string and parsed leniently below. Treating it as a number of seconds (the
// OAuth `expires_in` convention) would silently cache every token as already
// expired, re-prompting on every command.
type LoginResponse struct {
	Token     string `json:"token"`
	TenantID  string `json:"tenant_id"`
	ExpiresAt string `json:"expires_at"`
}

// expiry parses ExpiresAt into a time, or returns the zero time when it is
// absent or unparseable.
//
// A zero expiry means "cache without an expiry check", which is the right
// failure mode: the server is the authority on whether a token is still good,
// and it answers with a 401 that sets Force and re-authenticates. Refusing to
// cache at all would instead re-prompt on every single command.
//
// The server emits ISO-8601 UTC; Python's isoformat() may render that with a
// "+00:00" offset or a bare naive stamp depending on how the datetime was
// built, and neither is RFC3339-with-Z. Both spellings are tried rather than
// assuming one, because guessing wrong costs a prompt per command.
func (r *LoginResponse) Expiry() time.Time {
	raw := strings.TrimSpace(r.ExpiresAt)
	if raw == "" {
		return time.Time{}
	}
	for _, layout := range []string{
		time.RFC3339,          // 2026-09-25T10:00:00Z / +00:00
		"2006-01-02T15:04:05", // naive, no offset
		"2006-01-02T15:04:05.999999",
		"2006-01-02 15:04:05",
	} {
		if t, err := time.Parse(layout, raw); err == nil {
			return t.UTC()
		}
	}
	return time.Time{}
}

// SupportsForce opts this handler into restish's retry-once-after-401 path
// (auth.ForceCapable).
//
// This marker method is load-bearing, not decorative. Restish installs its
// OnUnauthorized callback *only* for handlers that implement ForceCapable
// (internal/cli/auth.go), so without it Authenticate is never called a second
// time and ac.Force is never true — which would make the whole
// "attach nothing, prompt on the real 401" flow described on the type above
// silently degrade into "never authenticate at all".
//
// Guarded by TestStandaloneAuthIsForceCapable, because nothing else in this
// package references it and it would otherwise look like dead code to a reader
// or a linter.
func (s *StandaloneAuth) SupportsForce() {}

// Compile-time proof that the marker is on the pointer type restish receives
// from AddAuthHandler. A value receiver here would type-assert differently.
var (
	_ auth.Handler      = (*StandaloneAuth)(nil)
	_ auth.ForceCapable = (*StandaloneAuth)(nil)
)

// WhoamiProbe is the subset of GET /auth/whoami we act on.
type WhoamiProbe struct {
	// AuthDisabled is true for a 503 whose detail is `auth_disabled`.
	AuthDisabled bool
	// loginInfo is the operator's pre-login message from a 401 body, if any.
	loginInfo string
}

// Authenticate attaches a bearer token to req, acquiring one if needed.
func (s *StandaloneAuth) Authenticate(ctx context.Context, req *http.Request, ac auth.AuthContext) error {
	if s.Now == nil {
		s.Now = time.Now
	}

	// 1. A cached, unexpired token is the fast path and by far the common one.
	//
	// Skipped when Force is set: restish sets it after a 401 so a retry can
	// re-authenticate rather than resend the token the server just rejected.
	// Without this branch a revoked or expired-server-side token would loop.
	if !ac.Force {
		if tok, err := ac.TokenStore.Get(ac.CacheKey); err == nil && tok != nil && tok.AccessToken != "" {
			if tok.Expiry.IsZero() || tok.Expiry.After(s.Now()) {
				req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
				return nil
			}
		}

		// 2. No usable cached token, and this is the first attempt: send the
		//    request unauthenticated. See the type comment — this is what keeps
		//    `sbs --help` and every unauthenticated endpoint prompt-free. If the
		//    server does want credentials it answers 401, and restish calls us
		//    again with Force set.
		return nil
	}

	base := strings.TrimRight(ac.BaseURL, "/")

	// 3. The Force pass: the request really did come back 401, so credentials
	//    are genuinely needed. Probe once first — the shim's _preflight — which
	//    answers two questions with one request: is auth switched off entirely
	//    (so this 401 came from something else), and does the operator have a
	//    message to show before the prompt.
	probe := s.Probe(ctx, base, ac)
	if probe.AuthDisabled {
		// mode: disabled. Attach nothing, prompt for nothing, and let the
		// request through — the server does not want credentials.
		return nil
	}

	// 4. Prompt, exchange, cache.
	if probe.loginInfo != "" {
		// sync.Once, not a bool: this must hold across the Force retry.
		s.loginInfoOnce.Do(func() { WriteLine(ac.Stderr, probe.loginInfo) })
	}

	username := strings.TrimSpace(ac.Params["username"])
	if username == "" {
		var err error
		username, err = ac.Prompter.Prompt("Username: ")
		if err != nil {
			return fmt.Errorf("could not read username: %w", err)
		}
		username = strings.TrimSpace(username)
	}
	password, err := ac.Prompter.PromptSecret("Password: ")
	if err != nil {
		return fmt.Errorf("could not read password: %w", err)
	}
	if username == "" || password == "" {
		return fmt.Errorf("username and password are required")
	}

	tok, err := s.login(ctx, base, username, password, ac.HTTPClient)
	if err != nil {
		return err
	}

	cached := auth.CachedToken{AccessToken: tok.Token, TokenType: "Bearer", Expiry: tok.Expiry()}
	// A token we cannot cache is still a token we can use for this request:
	// warn, do not fail. The alternative is refusing to run a command purely
	// because a cache directory is read-only.
	if err := ac.TokenStore.Set(ac.CacheKey, cached); err != nil {
		WriteLine(ac.Stderr, fmt.Sprintf("Warning: could not cache the access token: %v", err))
	}

	req.Header.Set("Authorization", "Bearer "+tok.Token)
	return nil
}

// probe performs the single GET /auth/whoami that decides between "auth is
// off", "auth is on and here is the operator's message" and "auth is on".
//
// Every failure mode returns a zero probe rather than an error: an unreachable
// or unexpected /auth/whoami must not stop us from trying to log in, because
// the login attempt produces a far better error message than the probe could.
func (s *StandaloneAuth) Probe(ctx context.Context, base string, ac auth.AuthContext) WhoamiProbe {
	client := ac.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/auth/whoami", nil)
	if err != nil {
		return WhoamiProbe{}
	}
	resp, err := client.Do(req)
	if err != nil {
		return WhoamiProbe{}
	}
	defer resp.Body.Close()

	// Bounded read: this is an unauthenticated endpoint on a host we may not
	// control yet (the user could have typo'd a URL into SBS_URL), so an
	// unbounded ReadAll is a memory-exhaustion vector on our own side.
	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return WhoamiProbe{}
	}

	var parsed struct {
		Detail    string `json:"detail"`
		LoginInfo string `json:"login_info"`
	}
	_ = json.Unmarshal(body, &parsed) // a non-JSON body just yields empty fields

	switch resp.StatusCode {
	case http.StatusServiceUnavailable:
		return WhoamiProbe{AuthDisabled: parsed.Detail == "auth_disabled"}
	case http.StatusUnauthorized:
		return WhoamiProbe{loginInfo: parsed.LoginInfo}
	}
	return WhoamiProbe{}
}

// login exchanges credentials for a bearer token via POST /auth/login.
func (s *StandaloneAuth) login(ctx context.Context, base, username, password string, client *http.Client) (*LoginResponse, error) {
	if client == nil {
		client = http.DefaultClient
	}

	body, err := json.Marshal(map[string]string{"username": username, "password": password})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/auth/login", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("could not reach %s: %w", base, err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return nil, err
	}

	if resp.StatusCode == http.StatusUnauthorized {
		// Surface the server's own machine-readable reason rather than a
		// paraphrase: `invalid_credentials` is what the docs and the UI say.
		var parsed struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &parsed)
		detail := parsed.Detail
		if detail == "" {
			detail = "invalid_credentials"
		}
		return nil, fmt.Errorf("login failed: %s", detail)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("login failed: %s returned HTTP %d", base+"/auth/login", resp.StatusCode)
	}

	var parsed LoginResponse
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("login failed: %s did not return valid JSON", base+"/auth/login")
	}
	if parsed.Token == "" {
		return nil, fmt.Errorf("login failed: server did not return a token")
	}
	return &parsed, nil
}
