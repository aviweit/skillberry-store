package tests

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/skillberry-ai/skillberry-store/client/go/cli"
)

// downloadServer stands in for a store's /cli/download endpoint.
//
// The store publishes an artifact's identity in response headers, so the fake
// does the same: one route, answering both HEAD (identity only) and GET (identity
// plus bytes). `status` lets a test drive the non-ready cases, which the real
// server signals with an HTTP status rather than a field in a document.
func downloadServer(t *testing.T, payload []byte, status int) (*httptest.Server, string) {
	t.Helper()
	sum := sha256.Sum256(payload)
	hexSum := hex.EncodeToString(sum[:])

	mux := http.NewServeMux()
	mux.HandleFunc("/cli/download", func(w http.ResponseWriter, r *http.Request) {
		platform := r.URL.Query().Get("platform")
		format := r.URL.Query().Get("format")

		if status != http.StatusOK {
			w.Header().Set("Vary", "User-Agent")
			w.Header().Set("X-SBS-CLI-Platform", platform)
			if status == http.StatusServiceUnavailable {
				w.Header().Set("Retry-After", "10")
			}
			w.WriteHeader(status)
			return
		}

		name := cli.CLIName
		if strings.HasPrefix(platform, "windows-") {
			name = cli.CLIName + ".exe"
		}
		if format == "archive" {
			name = fmt.Sprintf("%s-%s%s", cli.CLIName, platform, cli.ArchiveSuffix(platform))
		}

		w.Header().Set("X-SBS-SHA256", hexSum)
		w.Header().Set("X-SBS-CLI-Version", "9.9.9")
		w.Header().Set("X-SBS-CLI-Platform", platform)
		w.Header().Set("X-SBS-CLI-URL-Injection", "patch")
		w.Header().Set("ETag", `"`+hexSum+`"`)
		w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write(payload)
	})

	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, hexSum
}

func TestDownloadCLIWritesVerifiedExecutable(t *testing.T) {
	payload := []byte("#!/bin/sh\necho fake-sbs\n")
	srv, wantSum := downloadServer(t, payload, http.StatusOK)

	target := filepath.Join(t.TempDir(), "sbs")
	out, _ := captureFile(t)
	env := cli.MapEnviron{cli.URLEnvVar: srv.URL}

	code := cli.DoDownloadCLI([]string{"--output", target}, out, devNull(t), env)
	if code != 0 {
		t.Fatalf("cli.DoDownloadCLI = %d, want 0", code)
	}

	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("artifact not written: %v", err)
	}
	if string(got) != string(payload) {
		t.Errorf("artifact contents = %q, want the served payload", got)
	}
	sum := sha256.Sum256(got)
	if hex.EncodeToString(sum[:]) != wantSum {
		t.Error("written artifact does not match the published sha256")
	}

	if runtime.GOOS != "windows" {
		// A lost executable bit is the exact problem a browser download has and
		// this path exists to avoid (§5.5.2).
		info, err := os.Stat(target)
		if err != nil {
			t.Fatal(err)
		}
		if perm := info.Mode().Perm(); perm != 0o755 {
			t.Errorf("artifact mode = %o, want 755", perm)
		}
	}
}

// The supply-chain assertion (§5.8 #3, B18): a payload whose hash does not match
// the published digest must never reach the target path, let alone with +x set.
func TestDownloadCLIRefusesChecksumMismatch(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/cli/download", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-SBS-SHA256", strings.Repeat("0", 64)) // deliberately wrong
		w.Header().Set("X-SBS-CLI-Version", "9.9.9")
		w.Header().Set("X-SBS-CLI-Platform", r.URL.Query().Get("platform"))
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("tampered payload"))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	target := filepath.Join(t.TempDir(), "sbs")
	code := cli.DoDownloadCLI([]string{"--output", target}, devNull(t), devNull(t),
		cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code == 0 {
		t.Fatal("a checksum mismatch must fail")
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Error("a tampered artifact was written to the target path")
	}
}

// An artifact served with no digest cannot be verified, so installing it would be
// an unannounced trust jump.
func TestDownloadCLIRefusesAnUnverifiableArtifact(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/cli/download", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-SBS-CLI-Version", "9.9.9")
		w.Header().Set("X-SBS-CLI-Platform", r.URL.Query().Get("platform"))
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("unverifiable"))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	target := filepath.Join(t.TempDir(), "sbs")
	errOut, readErr := captureFile(t)
	code := cli.DoDownloadCLI([]string{"--output", target}, devNull(t), errOut,
		cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code == 0 {
		t.Fatal("an artifact with no published sha256 must not be installed")
	}
	if !strings.Contains(readErr(), "sha256") {
		t.Errorf("stderr = %q, want it to say why it refused", readErr())
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Error("an unverifiable artifact was written")
	}
}

func TestDownloadCLIReportsPreparingWithRetryHint(t *testing.T) {
	srv, _ := downloadServer(t, []byte("x"), http.StatusServiceUnavailable)
	errOut, readErr := captureFile(t)

	code := cli.DoDownloadCLI([]string{"--output", filepath.Join(t.TempDir(), "sbs")},
		devNull(t), errOut, cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code == 0 {
		t.Fatal("a preparing platform must not report success")
	}
	msg := readErr()
	if !strings.Contains(msg, "prepared") {
		t.Errorf("stderr = %q, want it to say the build is being prepared", msg)
	}
	// The Retry-After hint is what turns "try again" into actionable advice.
	if !strings.Contains(msg, "10") {
		t.Errorf("stderr = %q, want the retry-after seconds", msg)
	}
}

func TestDownloadCLIReportsAnUnavailablePlatform(t *testing.T) {
	srv, _ := downloadServer(t, []byte("x"), http.StatusNotFound)

	errOut, readErr := captureFile(t)
	code := cli.DoDownloadCLI(nil, devNull(t), errOut, cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code == 0 {
		t.Fatal("an unavailable platform must not report success")
	}
	if !strings.Contains(readErr(), cli.CurrentPlatform()) {
		t.Errorf("stderr = %q, want it to name the platform", readErr())
	}
}

// SBS_CLI_DOWNLOAD=off unregisters the route entirely, so the CLI sees a bare 404
// with none of the handler's headers. Saying so beats "no build available".
func TestDownloadCLISurfacesDownloadsDisabled(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(srv.Close)

	_, err := cli.Inspect(srv.URL, cli.CurrentPlatform(), "raw")
	if err == nil {
		t.Fatal("a bare 404 must be an error")
	}
	if !strings.Contains(err.Error(), "SBS_CLI_DOWNLOAD") {
		t.Errorf("error = %v, want it to name the operator switch", err)
	}
}

func TestInspectUnreachableStore(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // now refusing connections

	_, err := cli.Inspect(url, cli.CurrentPlatform(), "raw")
	if err == nil {
		t.Fatal("an unreachable store must be an error")
	}
	if !strings.Contains(err.Error(), "could not reach") {
		t.Errorf("error = %v, want a connection-level message naming the host", err)
	}
}

func TestInspectRejectsAnUnknownPlatform(t *testing.T) {
	srv, _ := downloadServer(t, []byte("x"), http.StatusBadRequest)
	_, err := cli.Inspect(srv.URL, "plan9-mips", "raw")
	if err == nil {
		t.Fatal("an unknown platform must be an error")
	}
	if !strings.Contains(err.Error(), "plan9-mips") {
		t.Errorf("error = %v, want it to name the platform asked for", err)
	}
}

// Inspect must read the digest and size without transferring the artifact: that
// is what makes it cheap enough to call before a 32 MB download.
func TestInspectUsesHeadAndReportsIdentity(t *testing.T) {
	payload := []byte("0123456789")
	var methods []string
	sum := sha256.Sum256(payload)
	hexSum := hex.EncodeToString(sum[:])

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.Method)
		w.Header().Set("X-SBS-SHA256", hexSum)
		w.Header().Set("X-SBS-CLI-Version", "1.2.3")
		w.Header().Set("X-SBS-CLI-URL-Injection", "sidecar")
		w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	info, err := cli.Inspect(srv.URL, "darwin-arm64", "raw")
	if err != nil {
		t.Fatalf("cli.Inspect: %v", err)
	}
	if len(methods) != 1 || methods[0] != http.MethodHead {
		t.Errorf("requests = %v, want exactly one HEAD", methods)
	}
	if info.SHA256 != hexSum {
		t.Errorf("SHA256 = %q, want %q", info.SHA256, hexSum)
	}
	if info.Size != int64(len(payload)) {
		t.Errorf("Size = %d, want %d", info.Size, len(payload))
	}
	if info.Version != "1.2.3" {
		t.Errorf("Version = %q, want 1.2.3", info.Version)
	}
	if info.URLInjection != "sidecar" {
		t.Errorf("URLInjection = %q, want sidecar", info.URLInjection)
	}
}

func TestDownloadCLIArchiveFormatUsesArchiveModeAndName(t *testing.T) {
	payload := []byte("fake tarball")
	srv, _ := downloadServer(t, payload, http.StatusOK)

	target := filepath.Join(t.TempDir(), "sbs.tar.gz")
	code := cli.DoDownloadCLI([]string{"--format", "archive", "--output", target},
		devNull(t), devNull(t), cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code != 0 {
		t.Fatalf("cli.DoDownloadCLI --format archive = %d", code)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(target)
		if err != nil {
			t.Fatal(err)
		}
		// An archive is data, not a program: it must not be +x.
		if perm := info.Mode().Perm(); perm != 0o644 {
			t.Errorf("archive mode = %o, want 644", perm)
		}
	}
}

func TestDownloadCLIRequestsTheSelectedVariant(t *testing.T) {
	// Which artifact you get is a query argument on the one endpoint, so the
	// request must carry both.
	var gotQuery string
	payload := []byte("x")
	sum := sha256.Sum256(payload)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("X-SBS-SHA256", hex.EncodeToString(sum[:]))
		w.Header().Set("X-SBS-CLI-Version", "1.0.0")
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write(payload)
	}))
	t.Cleanup(srv.Close)

	target := filepath.Join(t.TempDir(), "out")
	code := cli.DoDownloadCLI(
		[]string{"--platform", "linux-arm64", "--format", "archive", "--output", target},
		devNull(t), devNull(t), cli.MapEnviron{cli.URLEnvVar: srv.URL})
	if code != 0 {
		t.Fatalf("cli.DoDownloadCLI = %d", code)
	}
	for _, want := range []string{"platform=linux-arm64", "format=archive"} {
		if !strings.Contains(gotQuery, want) {
			t.Errorf("query = %q, want it to contain %q", gotQuery, want)
		}
	}
}

func TestDownloadCLIDefaultFilenames(t *testing.T) {
	payload := []byte("x")
	srv, _ := downloadServer(t, payload, http.StatusOK)

	// Run in a temp cwd so the default output path lands somewhere disposable.
	dir := t.TempDir()
	orig, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(orig) })

	if code := cli.DoDownloadCLI(nil, devNull(t), devNull(t),
		cli.MapEnviron{cli.URLEnvVar: srv.URL}); code != 0 {
		t.Fatalf("cli.DoDownloadCLI = %d", code)
	}
	want := cli.CLIName
	if runtime.GOOS == "windows" {
		want = cli.CLIName + ".exe"
	}
	if _, err := os.Stat(filepath.Join(dir, want)); err != nil {
		t.Errorf("default raw download should be named %q: %v", want, err)
	}
}

func TestSelfUpdateRejectsArchiveFormat(t *testing.T) {
	// Replacing the running binary with a tarball would produce a file that is
	// not executable; refusing beats "installed" followed by "command not found".
	code := cli.DoSelfUpdate([]string{"--format", "archive"}, devNull(t), devNull(t), cli.MapEnviron{})
	if code != 2 {
		t.Errorf("cli.DoSelfUpdate --format archive = %d, want 2", code)
	}
}

func TestSelfUpdateSkipsWhenVersionsMatch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodHead {
			t.Error("self-update must not download when the versions already match")
		}
		w.Header().Set("X-SBS-SHA256", strings.Repeat("a", 64))
		w.Header().Set("X-SBS-CLI-Version", cli.Version) // identical to this build
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	out, read := captureFile(t)
	if code := cli.DoSelfUpdate(nil, out, devNull(t), cli.MapEnviron{cli.URLEnvVar: srv.URL}); code != 0 {
		t.Fatalf("cli.DoSelfUpdate = %d, want 0", code)
	}
	if !strings.Contains(read(), "Already running") {
		t.Errorf("stdout = %q, want it to say no update was needed", read())
	}
}

func TestDownloadCLIHelpFlagExplainsUsage(t *testing.T) {
	for _, verb := range []string{"download-cli", "self-update"} {
		_, err := cli.ParseDistFlags([]string{"--help"}, verb)
		if err == nil {
			t.Fatalf("%s --help should produce usage text", verb)
		}
		if !strings.Contains(err.Error(), fmt.Sprintf("%s %s", cli.CLIName, verb)) {
			t.Errorf("%s --help = %v, want branded usage", verb, err)
		}
	}
}

// A busy store answers 429 on the transfer, not on the availability check: the
// endpoint budgets HEAD far more generously than GET, precisely so that reading a
// digest or polling for readiness does not spend the download allowance. That
// makes the GET the path a user actually sees rate-limited, so its message has to
// name the wait rather than print a bare status code.
func TestDownloadReportsRateLimitingWithTheServersWait(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodHead {
			w.Header().Set("X-SBS-CLI-Platform", "linux-amd64")
			w.Header().Set("X-SBS-SHA256", strings.Repeat("a", 64))
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("Retry-After", "42")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()

	info, err := cli.Inspect(srv.URL, "linux-amd64", "raw")
	if err != nil {
		t.Fatalf("Inspect should succeed -- a HEAD is not what gets limited: %v", err)
	}

	_, _, err = cli.DownloadVerified(srv.URL, info, "raw", filepath.Join(t.TempDir(), "sbs"))
	if err == nil {
		t.Fatal("a 429 must be reported as an error")
	}
	for _, want := range []string{"rate-limiting", "42 seconds"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the message should contain %q, got: %v", want, err)
		}
	}
	if strings.Contains(err.Error(), "HTTP 429") {
		t.Errorf("a bare status code is not actionable advice: %v", err)
	}
}

func TestDownloadFallsBackToADefaultWaitWhenTheServerGivesNone(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodHead {
			w.Header().Set("X-SBS-CLI-Platform", "linux-amd64")
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusTooManyRequests) // no Retry-After
	}))
	defer srv.Close()

	info, _ := cli.Inspect(srv.URL, "linux-amd64", "raw")
	_, _, err := cli.DownloadVerified(srv.URL, info, "raw", filepath.Join(t.TempDir(), "sbs"))
	if err == nil {
		t.Fatal("expected an error")
	}
	// Never "try again in  seconds".
	if !strings.Contains(err.Error(), "60 seconds") {
		t.Errorf("expected the fallback wait, got: %v", err)
	}
}
