package cli

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// The CLI downloading the CLI (§5.8).
//
// Implemented with net/http against /cli/download rather than as a generated
// operation, because a promoted API cannot build any command until it has
// fetched the store's spec — and these two verbs are what a user reaches for
// when that fetch is exactly what is failing. Routing them through the engine
// would make them unavailable in the situation they exist to fix.

// artifactInfo is what a HEAD on /cli/download reports about an artifact.
//
// The store publishes an artifact's identity in response headers, so one request
// answers "does this exist, how big is it, and what should it hash to" without
// transferring ~32 MB. A GET carries the same headers alongside the bytes, which
// is what lets DownloadVerified check the digest on the response it is already
// reading rather than trusting a separately-fetched value.
type artifactInfo struct {
	Platform     string
	SHA256       string
	Size         int64
	Version      string
	URLInjection string
	Filename     string
}

// CurrentPlatform is this binary's own platform id.
//
// runtime.GOOS/GOARCH is exact — unlike the server-side User-Agent sniffing of
// §5.6, which cannot tell Apple Silicon from Intel. So the CLI always sends an
// explicit ?platform=, and never relies on detection.
func CurrentPlatform() string {
	return runtime.GOOS + "-" + runtime.GOARCH
}

// downloadURL builds the single endpoint's URL for one variant.
func downloadURL(base, platform, format string) string {
	q := url.Values{}
	q.Set("platform", platform)
	q.Set("format", format)
	return strings.TrimRight(base, "/") + "/cli/download?" + q.Encode()
}

// DoDownloadCLI implements `sbs download-cli`.
func DoDownloadCLI(args []string, stdout, stderr *os.File, env Environ) int {
	opts, err := ParseDistFlags(args, "download-cli")
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 2
	}

	base := BaseURL(env)
	plat := opts.Platform
	if plat == "" {
		plat = CurrentPlatform()
	}

	info, err := Inspect(base, plat, opts.Format)
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 1
	}

	target := opts.Output
	if target == "" {
		target = info.Filename
		if target == "" {
			target = CLIName
		}
		if opts.Format == "archive" {
			target = fmt.Sprintf("%s-%s%s", CLIName, plat, ArchiveSuffix(plat))
		}
	}

	written, sum, err := DownloadVerified(base, info, opts.Format, target)
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 1
	}

	fmt.Fprintf(stdout, "Downloaded %s %s for %s to %s (%d bytes, sha256 %s)\n",
		CLIName, info.Version, plat, target, written, sum)
	if opts.Format != "archive" && info.URLInjection == "sidecar" {
		// The one platform/mechanism combination that needs a second file
		// (§5.2): the binary itself carries no baked URL, so a raw download
		// comes up pointing at its compile-time default.
		fmt.Fprintf(stdout,
			"\nNote: builds for %s carry their store URL in a sidecar file rather than in\n"+
				"the binary. Download --format archive instead, or run:\n  %s connect %s\n",
			plat, target, base)
	}
	return 0
}

// DoSelfUpdate implements `sbs self-update`: replace the running binary.
func DoSelfUpdate(args []string, stdout, stderr *os.File, env Environ) int {
	opts, err := ParseDistFlags(args, "self-update")
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 2
	}
	if opts.Format == "archive" {
		WriteLine(stderr, fmt.Sprintf("%s: self-update cannot use --format archive", CLIName))
		return 2
	}

	// Always this platform: self-update replaces *this* binary, so honouring a
	// --platform here would install an artifact that cannot execute.
	plat := CurrentPlatform()
	base := BaseURL(env)

	self, err := os.Executable()
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: cannot locate the running binary: %v", CLIName, err))
		return 1
	}
	// Resolve symlinks so an update through ~/.local/bin/sbs -> /opt/sbs/sbs
	// replaces the real file rather than turning the symlink into a regular one.
	if resolved, err := filepath.EvalSymlinks(self); err == nil {
		self = resolved
	}

	info, err := Inspect(base, plat, "raw")
	if err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 1
	}

	if info.Version != "" && info.Version == Version {
		fmt.Fprintf(stdout, "Already running %s %s; nothing to do.\n", CLIName, Version)
		return 0
	}

	// Staged in the target's own directory: os.Rename cannot cross filesystems,
	// and /tmp is very often a different one (tmpfs, or a separate volume).
	staged := self + ".new"
	if _, _, err := DownloadVerified(base, info, "raw", staged); err != nil {
		WriteLine(stderr, fmt.Sprintf("%s: %v", CLIName, err))
		return 1
	}

	if runtime.GOOS == "windows" {
		// Windows refuses to replace a file that is currently mapped as a
		// running image, so the rename has to happen after this process exits.
		// Printing the command is honest; silently scheduling a background
		// mover would be worse than telling the user one line to run.
		fmt.Fprintf(stdout,
			"Downloaded %s %s to %s.\nWindows cannot replace a running executable, so finish with:\n  move /Y \"%s\" \"%s\"\n",
			CLIName, info.Version, staged, staged, self)
		return 0
	}

	if err := os.Rename(staged, self); err != nil {
		os.Remove(staged)
		WriteLine(stderr, fmt.Sprintf("%s: could not replace %s: %v", CLIName, self, err))
		return 1
	}
	fmt.Fprintf(stdout, "Updated %s to %s at %s\n", CLIName, info.Version, self)
	return 0
}

// DistOptions is the parsed flag set shared by both verbs.
type DistOptions struct {
	Platform string
	Output   string
	Format   string
}

// ParseDistFlags parses the flags by hand rather than with the flag package.
//
// Two reasons: these verbs run before cobra exists, and `flag` would print its
// own unbranded usage to stderr and call os.Exit on a bad value, which would
// bypass the error handling the rest of this program uses.
func ParseDistFlags(args []string, verb string) (DistOptions, error) {
	opts := DistOptions{Format: "raw"}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		// Accept both `--flag value` and `--flag=value`: the second is what
		// people type, and rejecting it would be a gratuitous difference from
		// every generated operation on this same CLI.
		name, inline, hasInline := strings.Cut(arg, "=")
		value := func() (string, error) {
			if hasInline {
				return inline, nil
			}
			if i+1 >= len(args) {
				return "", fmt.Errorf("%s: %s needs a value", verb, name)
			}
			i++
			return args[i], nil
		}

		var err error
		switch name {
		case "--platform":
			opts.Platform, err = value()
		case "--output", "-o":
			opts.Output, err = value()
		case "--format":
			opts.Format, err = value()
		case "--help", "-h":
			return opts, fmt.Errorf("usage: %s %s [--platform <id>] [--output <path>] [--format raw|archive]", CLIName, verb)
		default:
			return opts, fmt.Errorf("%s: unknown option %q", verb, arg)
		}
		if err != nil {
			return opts, err
		}
	}
	if opts.Format != "raw" && opts.Format != "archive" {
		return opts, fmt.Errorf("%s: --format must be raw or archive (got %q)", verb, opts.Format)
	}
	return opts, nil
}

// Inspect asks the store what it would serve for a platform, without
// transferring it.
//
// One HEAD against the download endpoint. The non-ready states come back as HTTP
// statuses rather than as a field in a document, so they turn into the actionable
// messages §5.8 #2 asks for.
func Inspect(base, platform, format string) (artifactInfo, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest(http.MethodHead, downloadURL(base, platform, format), nil)
	if err != nil {
		return artifactInfo{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return artifactInfo{}, fmt.Errorf("could not reach %s: %w", base, err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		// fall through
	case http.StatusNotFound:
		// Either the whole surface is off (SBS_CLI_DOWNLOAD=off unregisters the
		// route) or this one platform has no artifact. The response body is
		// empty on a HEAD, so the two are distinguished by the header the
		// handler sets only when it recognised the platform.
		if resp.Header.Get("X-SBS-CLI-Platform") == "" && resp.Header.Get("Vary") == "" {
			return artifactInfo{}, fmt.Errorf(
				"%s does not offer CLI downloads (the operator may have set SBS_CLI_DOWNLOAD=off)", base)
		}
		return artifactInfo{}, fmt.Errorf(
			"%s has no %s build available", base, platform)
	case http.StatusServiceUnavailable:
		return artifactInfo{}, fmt.Errorf(
			"the %s build is still being prepared; try again in %s seconds",
			platform, retryAfter(resp, "10"))
	case http.StatusBadRequest:
		return artifactInfo{}, fmt.Errorf(
			"%s does not recognise the platform %q", base, platform)
	case http.StatusTooManyRequests:
		return artifactInfo{}, fmt.Errorf(
			"%s is rate-limiting downloads; try again in %s seconds", base, retryAfter(resp, "60"))
	default:
		return artifactInfo{}, fmt.Errorf("%s returned HTTP %d", downloadURL(base, platform, format), resp.StatusCode)
	}

	info := artifactInfo{
		Platform:     platform,
		SHA256:       resp.Header.Get("X-SBS-SHA256"),
		Version:      resp.Header.Get("X-SBS-CLI-Version"),
		URLInjection: resp.Header.Get("X-SBS-CLI-URL-Injection"),
		Filename:     filenameFor(platform, format),
	}
	if raw := resp.Header.Get("Content-Length"); raw != "" {
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
			info.Size = n
		}
	}
	return info, nil
}

// filenameFor is the artifact's natural name on disk for a platform and format.
func filenameFor(platform, format string) string {
	if format == "archive" {
		return fmt.Sprintf("%s-%s%s", CLIName, platform, ArchiveSuffix(platform))
	}
	if strings.HasPrefix(platform, "windows-") {
		return CLIName + ".exe"
	}
	return CLIName
}

// DownloadVerified streams the artifact to a temp file beside the target,
// verifies its sha256 against what the store published, then renames it into
// place.
//
// Verifying before the rename is the whole point (§5.8 #3, B18): the store is
// handing the user an executable, so a truncated or tampered download must never
// end up at the target path, let alone with the executable bit set.
//
// The expected digest is read from the GET's own response headers, not from the
// earlier HEAD, so there is no window in which the two could describe different
// bytes. Returns the byte count and the hex digest actually computed.
// retryAfter reads the server's own Retry-After, falling back to a sane default
// so the advice is never "try again in  seconds".
func retryAfter(resp *http.Response, fallback string) string {
	if wait := resp.Header.Get("Retry-After"); wait != "" {
		return wait
	}
	return fallback
}

func DownloadVerified(base string, info artifactInfo, format, target string) (int64, string, error) {
	dir := filepath.Dir(target)
	if dir == "" {
		dir = "."
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return 0, "", err
	}

	client := &http.Client{Timeout: 10 * time.Minute}
	resp, err := client.Get(downloadURL(base, info.Platform, format))
	if err != nil {
		return 0, "", fmt.Errorf("download failed: %w", err)
	}
	defer resp.Body.Close()
	// The transfer is the rate-limited half of the endpoint -- reading availability
	// with a HEAD is budgeted far more generously -- so this is where a busy store
	// answers 429, and the message has to say what to do about it.
	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusServiceUnavailable:
		return 0, "", fmt.Errorf("the build is still being prepared; try again in %s seconds",
			retryAfter(resp, "10"))
	case http.StatusTooManyRequests:
		return 0, "", fmt.Errorf(
			"%s is rate-limiting downloads; try again in %s seconds", base, retryAfter(resp, "60"))
	default:
		return 0, "", fmt.Errorf("download failed: the store returned HTTP %d", resp.StatusCode)
	}

	want := resp.Header.Get("X-SBS-SHA256")
	if want == "" {
		want = info.SHA256
	}

	tmp, err := os.CreateTemp(dir, ".sbs-download-*")
	if err != nil {
		return 0, "", err
	}
	tmpName := tmp.Name()
	// Runs on every path; a no-op once the rename below has moved the file.
	defer os.Remove(tmpName)

	hasher := sha256.New()
	written, err := io.Copy(io.MultiWriter(tmp, hasher), resp.Body)
	if err != nil {
		tmp.Close()
		return 0, "", fmt.Errorf("download failed: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return 0, "", err
	}

	sum := hex.EncodeToString(hasher.Sum(nil))
	if want == "" {
		return 0, "", fmt.Errorf(
			"%s published no sha256 for this artifact; refusing to install unverified bytes", base)
	}
	if !strings.EqualFold(sum, want) {
		return 0, "", fmt.Errorf(
			"checksum mismatch: the store published %s but the download hashed to %s; not installing it",
			want, sum)
	}

	// 0755 for a raw binary — the browser-download problem this exists to avoid
	// (§5.5.2) is precisely a lost executable bit. An archive is data, so 0644.
	mode := os.FileMode(0o755)
	if format == "archive" {
		mode = 0o644
	}
	if err := os.Chmod(tmpName, mode); err != nil {
		return 0, "", err
	}
	if err := os.Rename(tmpName, target); err != nil {
		return 0, "", fmt.Errorf("could not write %s: %w", target, err)
	}
	return written, sum, nil
}

// ArchiveSuffix is the archive extension per platform, matching what the server
// generates in §5.5.2.
func ArchiveSuffix(platform string) string {
	if strings.HasPrefix(platform, "windows-") {
		return ".zip"
	}
	return ".tar.gz"
}
