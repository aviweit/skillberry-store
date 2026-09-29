package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/rest-sh/restish/v2/auth"
	"github.com/skillberry-ai/skillberry-store/client/go/cli"
)

// §8.1 #4 — the assertions inherited from the deleted
// src/skillberry_store/tests/cli/test_sdk_cli_login_info.py, so the
// docs/design/login-info.md §7 contract keeps a test after the Python shim is
// gone.

// fakeTokenStore is an in-memory auth.TokenStore.
type fakeTokenStore struct {
	tokens  map[string]auth.CachedToken
	setErr  error
	sets    int
	deletes []string
}

func newFakeTokenStore() *fakeTokenStore {
	return &fakeTokenStore{tokens: map[string]auth.CachedToken{}}
}

func (f *fakeTokenStore) Get(key string) (*auth.CachedToken, error) {
	tok, ok := f.tokens[key]
	if !ok {
		return nil, nil
	}
	return &tok, nil
}

func (f *fakeTokenStore) Set(key string, token auth.CachedToken) error {
	if f.setErr != nil {
		return f.setErr
	}
	f.sets++
	f.tokens[key] = token
	return nil
}

func (f *fakeTokenStore) Delete(key string) error {
	f.deletes = append(f.deletes, key)
	delete(f.tokens, key)
	return nil
}

func (f *fakeTokenStore) DeletePrefix(prefix string) error {
	for k := range f.tokens {
		if strings.HasPrefix(k, prefix) {
			delete(f.tokens, k)
		}
	}
	return nil
}

// fakePrompter records how many times it was asked, which is how the
// "never prompt proactively" and "print login_info once, before the first
// prompt" rules are actually verified.
type fakePrompter struct {
	username string
	password string
	prompts  int
	secrets  int
	err      error
}

func (f *fakePrompter) Prompt(string) (string, error) {
	f.prompts++
	return f.username, f.err
}

func (f *fakePrompter) PromptSecret(string) (string, error) {
	f.secrets++
	return f.password, f.err
}

// authFixture wires a handler against a test server.
type authFixture struct {
	handler  *cli.StandaloneAuth
	store    *fakeTokenStore
	prompter *fakePrompter
	stderr   *bytes.Buffer
	server   *httptest.Server
}

func (f *authFixture) ctx(force bool) auth.AuthContext {
	return auth.AuthContext{
		APIName:     cli.APIName,
		ProfileName: "default",
		BaseURL:     f.server.URL,
		CacheKey:    cli.APIName + ":default",
		Params:      map[string]string{},
		TokenStore:  f.store,
		Prompter:    f.prompter,
		Stderr:      f.stderr,
		HTTPClient:  f.server.Client(),
		Force:       force,
	}
}

func (f *authFixture) authenticate(t *testing.T, force bool) (*http.Request, error) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, f.server.URL+"/skills", nil)
	if err != nil {
		t.Fatal(err)
	}
	return req, f.handler.Authenticate(context.Background(), req, f.ctx(force))
}

// newAuthFixture builds a fixture around a handler func for /auth/whoami and
// /auth/login.
func newAuthFixture(t *testing.T, h http.HandlerFunc) *authFixture {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return &authFixture{
		handler:  &cli.StandaloneAuth{},
		store:    newFakeTokenStore(),
		prompter: &fakePrompter{username: "alice", password: "s3cret"},
		stderr:   &bytes.Buffer{},
		server:   srv,
	}
}

// storeHandler serves the two auth endpoints with configurable behaviour.
type storeBehaviour struct {
	whoamiStatus int
	whoamiBody   map[string]any
	loginStatus  int
	loginBody    map[string]any
	whoamiCalls  *int
	loginCalls   *int
	gotLogin     *map[string]string
}

func (b storeBehaviour) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/whoami":
			if b.whoamiCalls != nil {
				*b.whoamiCalls++
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(b.whoamiStatus)
			_ = json.NewEncoder(w).Encode(b.whoamiBody)
		case "/auth/login":
			if b.loginCalls != nil {
				*b.loginCalls++
			}
			if b.gotLogin != nil {
				var body map[string]string
				_ = json.NewDecoder(r.Body).Decode(&body)
				*b.gotLogin = body
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(b.loginStatus)
			_ = json.NewEncoder(w).Encode(b.loginBody)
		default:
			w.WriteHeader(http.StatusOK)
		}
	}
}

// TestStandaloneAuthIsForceCapable pins the marker method that makes the whole
// flow work. Without auth.ForceCapable, restish never installs its
// retry-after-401 callback, Force is never true, and the handler would never
// authenticate at all — a failure that no other test in this file would catch,
// because they all set Force explicitly.
func TestStandaloneAuthIsForceCapable(t *testing.T) {
	var h auth.Handler = &cli.StandaloneAuth{}
	if _, ok := h.(auth.ForceCapable); !ok {
		t.Fatal("cli.StandaloneAuth must implement auth.ForceCapable, or restish will " +
			"never retry after a 401 and the CLI will never prompt for credentials")
	}
}

func TestAuthReusesCachedToken(t *testing.T) {
	whoami := 0
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiCalls:  &whoami,
	}.handler())
	f.store.tokens[cli.APIName+":default"] = auth.CachedToken{AccessToken: "cached-tok"}

	req, err := f.authenticate(t, false)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer cached-tok" {
		t.Errorf("Authorization = %q, want the cached token", got)
	}
	if f.prompter.prompts != 0 || f.prompter.secrets != 0 {
		t.Error("a cached token must not prompt")
	}
	if whoami != 0 {
		t.Errorf("whoami was probed %d times; a cached token needs no probe", whoami)
	}
}

func TestAuthIgnoresExpiredCachedToken(t *testing.T) {
	login := 0
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "invalid_token"},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"token": "fresh-tok", "tenant_id": "alice"},
		loginCalls:   &login,
	}.handler())
	f.handler.Now = func() time.Time { return time.Unix(2000, 0) }
	f.store.tokens[cli.APIName+":default"] = auth.CachedToken{
		AccessToken: "stale-tok",
		Expiry:      time.Unix(1000, 0), // already expired relative to now
	}

	// Force=false: an expired cached token is not usable, and the handler must
	// fall through to "send unauthenticated" rather than attach a dead token.
	req, err := f.authenticate(t, false)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want nothing for an expired token on the first pass", got)
	}
	if login != 0 {
		t.Error("the first pass must not log in; that is what the 401 retry is for")
	}
}

// The "never prompt proactively" property. This is what keeps `sbs --help` from
// asking for a password just to fetch /openapi.json — an endpoint in the store's
// unauthenticated allow-list.
func TestAuthFirstPassSendsNothingAndNeverPrompts(t *testing.T) {
	whoami, login := 0, 0
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		whoamiCalls:  &whoami,
		loginCalls:   &login,
	}.handler())

	req, err := f.authenticate(t, false)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want no header on the unforced first pass", got)
	}
	if f.prompter.prompts != 0 || f.prompter.secrets != 0 {
		t.Errorf("prompted %d/%d times on the first pass; it must never prompt proactively",
			f.prompter.prompts, f.prompter.secrets)
	}
	if whoami != 0 || login != 0 {
		t.Errorf("whoami=%d login=%d; the first pass must make no auth calls at all", whoami, login)
	}
}

// §8.1 #4 — `503 auth_disabled` -> no header and no prompt.
func TestAuthDisabledAttachesNothingAndNeverPrompts(t *testing.T) {
	login := 0
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusServiceUnavailable,
		whoamiBody:   map[string]any{"detail": "auth_disabled"},
		loginCalls:   &login,
	}.handler())

	// Force=true is the interesting case: even when restish insists on
	// re-authenticating, a store with auth switched off must not be handed
	// credentials, and the user must not be asked for any.
	req, err := f.authenticate(t, true)
	if err != nil {
		t.Fatalf("Authenticate with auth disabled should succeed, got %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want no header when auth is disabled", got)
	}
	if f.prompter.prompts != 0 || f.prompter.secrets != 0 {
		t.Error("auth_disabled must not prompt: the server would ignore the credentials")
	}
	if login != 0 {
		t.Error("auth_disabled must not POST /auth/login")
	}
}

// §8.1 #4 — a 401 carrying login_info prints the operator's message to stderr
// exactly ONCE, and before the first prompt.
func TestLoginInfoPrintedOnceBeforeFirstPrompt(t *testing.T) {
	const banner = "Use your corporate SSO password. Contact #help for access."
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token", "login_info": banner},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"token": "tok-1", "tenant_id": "alice"},
	}.handler())

	// A prompter that captures the stderr contents at the moment it is first
	// called — the only way to assert ordering rather than mere presence.
	var stderrAtPrompt string
	f.prompter = &fakePrompter{username: "alice", password: "pw"}
	handler := f.handler
	orderingPrompter := &orderCapturingPrompter{
		inner:  f.prompter,
		stderr: f.stderr,
		seen:   &stderrAtPrompt,
	}

	req, _ := http.NewRequest(http.MethodGet, f.server.URL+"/skills", nil)
	ac := f.ctx(true)
	ac.Prompter = orderingPrompter
	if err := handler.Authenticate(context.Background(), req, ac); err != nil {
		t.Fatalf("Authenticate: %v", err)
	}

	if !strings.Contains(stderrAtPrompt, banner) {
		t.Errorf("login_info was not on stderr before the first prompt; stderr was %q", stderrAtPrompt)
	}

	// Second call (a later Force retry in the same process) must NOT reprint it.
	req2, _ := http.NewRequest(http.MethodGet, f.server.URL+"/skills", nil)
	ac2 := f.ctx(true)
	ac2.Prompter = orderingPrompter
	if err := handler.Authenticate(context.Background(), req2, ac2); err != nil {
		t.Fatalf("second Authenticate: %v", err)
	}
	if n := strings.Count(f.stderr.String(), banner); n != 1 {
		t.Errorf("login_info appeared %d times on stderr, want exactly 1 "+
			"(the UI shows its banner pre-attempt only, and the CLI must match)", n)
	}
}

// orderCapturingPrompter records stderr's contents the first time it is asked
// for anything.
type orderCapturingPrompter struct {
	inner  auth.Prompter
	stderr *bytes.Buffer
	seen   *string
	called bool
}

func (p *orderCapturingPrompter) capture() {
	if !p.called {
		p.called = true
		*p.seen = p.stderr.String()
	}
}

func (p *orderCapturingPrompter) Prompt(s string) (string, error) {
	p.capture()
	return p.inner.Prompt(s)
}

func (p *orderCapturingPrompter) PromptSecret(s string) (string, error) {
	p.capture()
	return p.inner.PromptSecret(s)
}

func TestNoLoginInfoMeansNoExtraStderr(t *testing.T) {
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"token": "tok", "tenant_id": "alice"},
	}.handler())

	if _, err := f.authenticate(t, true); err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if f.stderr.Len() != 0 {
		t.Errorf("stderr = %q, want nothing when the operator configured no message", f.stderr.String())
	}
}

// §8.1 #4 — a successful login stores the token and attaches it.
func TestSuccessfulLoginStoresAndAttachesToken(t *testing.T) {
	var sent map[string]string
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusOK,
		loginBody: map[string]any{
			"token":      "issued-tok",
			"tenant_id":  "alice",
			"expires_at": "2099-01-01T00:00:00Z",
		},
		gotLogin: &sent,
	}.handler())

	req, err := f.authenticate(t, true)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer issued-tok" {
		t.Errorf("Authorization = %q, want the issued token", got)
	}
	if sent["username"] != "alice" || sent["password"] != "s3cret" {
		t.Errorf("POST /auth/login body = %v, want the prompted credentials", sent)
	}
	cached, _ := f.store.Get(cli.APIName + ":default")
	if cached == nil || cached.AccessToken != "issued-tok" {
		t.Errorf("token was not cached: %+v", cached)
	}
	if cached != nil && cached.Expiry.IsZero() {
		t.Error("expires_at was not parsed into the cached token's Expiry")
	}
}

// expires_at is an ISO-8601 instant, not an `expires_in` duration. Reading it as
// seconds would cache every token as already expired and re-prompt on every
// command — a bug that is invisible until you run two commands in a row.
func TestLoginResponseExpiryParsing(t *testing.T) {
	cases := []struct {
		in   string
		zero bool
	}{
		{"2099-01-01T00:00:00Z", false},
		{"2099-01-01T00:00:00+00:00", false},
		{"2099-01-01T00:00:00", false},
		{"2099-01-01T00:00:00.123456", false},
		{"2099-01-01 00:00:00", false},
		{"", true},
		{"not-a-time", true},
		{"3600", true}, // an `expires_in`-style value must not be mistaken for an instant
	}
	for _, c := range cases {
		got := (&cli.LoginResponse{ExpiresAt: c.in}).Expiry()
		if got.IsZero() != c.zero {
			t.Errorf("expiry(%q).IsZero() = %v, want %v", c.in, got.IsZero(), c.zero)
		}
	}
	// A parsed expiry must be in the future for a future timestamp — i.e. it is
	// an absolute instant, not an offset from now.
	got := (&cli.LoginResponse{ExpiresAt: "2099-01-01T00:00:00Z"}).Expiry()
	if got.Year() != 2099 {
		t.Errorf("expiry year = %d, want 2099 (expires_at is an instant)", got.Year())
	}
}

func TestLoginFailureSurfacesServerDetail(t *testing.T) {
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusUnauthorized,
		loginBody:    map[string]any{"detail": "invalid_credentials"},
	}.handler())

	_, err := f.authenticate(t, true)
	if err == nil {
		t.Fatal("a rejected login must return an error")
	}
	// The server's own machine-readable reason, not a paraphrase: it is what the
	// docs and the UI say, so a user searching for it finds the right page.
	if !strings.Contains(err.Error(), "invalid_credentials") {
		t.Errorf("error = %v, want it to name invalid_credentials", err)
	}
	if f.store.sets != 0 {
		t.Error("a failed login must not cache anything")
	}
}

func TestLoginWithoutTokenInResponseFails(t *testing.T) {
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"tenant_id": "alice"}, // no token
	}.handler())

	if _, err := f.authenticate(t, true); err == nil {
		t.Fatal("a 200 with no token must be an error, not a silent success")
	}
}

// A token cache we cannot write to must not break the command: the token is
// still good for this request. Refusing to run because a cache directory is
// read-only would be a self-inflicted outage.
func TestUncacheableTokenStillAuthenticates(t *testing.T) {
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"token": "tok", "tenant_id": "alice"},
	}.handler())
	f.store.setErr = fmt.Errorf("read-only file system")

	req, err := f.authenticate(t, true)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer tok" {
		t.Errorf("Authorization = %q, want the token despite the cache failure", got)
	}
	if !strings.Contains(f.stderr.String(), "could not cache") {
		t.Errorf("stderr = %q, want a warning about the cache failure", f.stderr.String())
	}
}

// A configured username means only the password is prompted for. There is
// deliberately no `password` parameter: a password in a config file is a
// password in a plaintext file.
func TestConfiguredUsernameSkipsThePrompt(t *testing.T) {
	var sent map[string]string
	f := newAuthFixture(t, storeBehaviour{
		whoamiStatus: http.StatusUnauthorized,
		whoamiBody:   map[string]any{"detail": "missing_token"},
		loginStatus:  http.StatusOK,
		loginBody:    map[string]any{"token": "tok", "tenant_id": "bob"},
		gotLogin:     &sent,
	}.handler())

	req, _ := http.NewRequest(http.MethodGet, f.server.URL+"/skills", nil)
	ac := f.ctx(true)
	ac.Params = map[string]string{"username": "bob"}
	if err := f.handler.Authenticate(context.Background(), req, ac); err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if f.prompter.prompts != 0 {
		t.Error("a configured username must not be prompted for")
	}
	if f.prompter.secrets != 1 {
		t.Errorf("password prompted %d times, want 1", f.prompter.secrets)
	}
	if sent["username"] != "bob" {
		t.Errorf("username sent = %q, want the configured bob", sent["username"])
	}

	// And no password parameter is offered, so it cannot be put in config.
	for _, p := range f.handler.Parameters() {
		if p.Name == "password" {
			t.Error("Parameters() offers a `password` knob; credentials must not be storable in config")
		}
	}
}

// An unreachable store must not prompt: the probe fails, and prompting would
// collect credentials only to throw them away on a connection error.
func TestUnreachableStoreDoesNotPromptOnFirstPass(t *testing.T) {
	f := newAuthFixture(t, storeBehaviour{whoamiStatus: http.StatusOK}.handler())
	f.server.Close() // now unreachable

	req, err := f.authenticate(t, false)
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if f.prompter.prompts != 0 || f.prompter.secrets != 0 {
		t.Error("an unreachable store must not prompt on the first pass")
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want nothing", got)
	}
}
