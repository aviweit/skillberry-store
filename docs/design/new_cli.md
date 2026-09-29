# Native `sbs` CLI — embedded restish, a branded surface, and per-URL builds

Status: **Proposed**
Owner: skillberry-store
Scope: `skillberry-store` — two usability changes to the `sbs` CLI: (A) remove every user-visible mention of `restish`, so `sbs` reads as the one command a user needs; (B) serve the CLI from the server as a **native single-file executable** for five platforms, pre-configured with the deployment's own public URL, downloadable without authentication from the API, the UI, and the CLI itself.

**Central decision:** integrate restish as a **Go library** rather than as a subprocess. restish v2 publishes a supported embedding API for exactly this ([`github.com/rest-sh/restish/v2`](https://github.com/rest-sh/restish/blob/v2.3.0/restish.go), design record [042-custom-cli-embedding-surface.md](https://github.com/rest-sh/restish/blob/v2.3.0/docs/design/042-custom-cli-embedding-surface.md), example [`examples/example-cli`](https://github.com/rest-sh/restish/tree/v2.3.0/examples/example-cli)). That turns Feature A from a 150-line output rewriter into four configuration calls, and it removes Feature B's central blocker: Go cross-compiles all five platforms from the server's own container. The PyInstaller-over-the-Python-shim design that this supersedes is recorded, with its measurements, in §10.1; a second review question — restish as a C shared library behind a Python CLI — is recorded in §10.2.

Related: [access-control.md](access-control.md) (§8 the PEP as a router dependency, §10.1 the CLI/auth story), [login-info.md](login-info.md) (§7 the CLI's preflight probe), [npx.md](npx.md) (§5.11 `SBS_PUBLIC_URL` — adopted unchanged), [portable_storage.md](portable_storage.md), [build_concepts.md](build_concepts.md) (stamps and change detection).

---

## 0. Decisions at a glance

| # | Question | Decision |
| --- | --- | --- |
| D1 | How do we integrate restish? | **As a Go library.** `restish.New()` + `SetCommandName` + `SetCommandDescription` + `SetCommandSurface{PromotedAPI}`; the shim's subprocess model goes away (§3) |
| D2 | How do we remove "restish" from the output? | We don't rewrite it — we **never emit it**. The library takes the command name, the root description, the config/cache dirs and the support-command layout from us (§4.1–§4.4) |
| D3 | What is left saying "restish"? | Exactly **four strings**, measured: two global flag descriptions, `doctor`'s version label and its `shell setup` hint, plus the config *filename*. Resolutions in §3.3 (one upstream PR, one env var, one surface option) |
| D4 | Does the Python CLI code get rewritten in Go? | Yes, but it is **mostly deleted, not ported**: ~390 lines of subprocess glue become ~150–200 lines of Go, and the planned output scrubber is never written (§4.5) |
| D5 | How does login work without the shim's interception? | A custom `auth.Handler` (restish's public auth API) prompts, calls `POST /auth/login`, and caches the token in restish's own `TokenStore`. Auth happens on demand; `login`/`logout` survive only as thin verbs (§4.3) |
| D6 | What happens to the Python CLI? | **It is deleted.** One codebase, in Go. `pip` keeps an `sbs` through **platform wheels** carrying the Go binary (the `ruff`/`uv` pattern); nothing Python remains to maintain, and no path still shells out to restish (§4.6) |
| D7 | Can the server produce all five platforms? | **Yes** — `CGO_ENABLED=0` cross-compilation, verified for all five targets from one Linux box (§3.2). This is what the PyInstaller design could not do |
| D8 | How is `SBS_PUBLIC_URL` baked in? | Two mechanisms, verified: **re-link** with `-ldflags -X` when a Go toolchain is present (2.3 s per platform), or **in-place slot patching** of a CI-built artifact when it is not (0 size delta, no relink). Per-platform choice in §5.2 |
| D9 | Is a Go toolchain required in the runtime image? | **No.** The default path is CI-built artifacts + slot patching; a toolchain is an opt-in image variant that upgrades every platform to a real per-URL build (§5.4) |
| D10 | How is "already prepared for this URL" detected? | A `manifest.json` stamp keyed on `(public_url, cli_commit, restish_version, platform, mechanism)` — same idiom as the repo's `.stamps/` (§5.3) |
| D11 | How is the download endpoint unauthenticated "independently of ACL mode"? | A new **mandatory floor** (`_ALWAYS_UNAUTH_PATHS`) merged into `unauthenticated_paths` regardless of the operator's config — which also fixes an existing footgun where their list *replaces* the defaults (§5.5.3) |
| D12 | How is the browser's platform detected? | `platform` param wins; then UA Client Hints (advertised with `Accept-CH`); then User-Agent. Apple Silicon vs Intel is undecidable from UA, so the UI resolves it with `navigator.userAgentData` and always shows a chooser (§5.6) |
| D13 | Can the CLI download the CLI? | Yes — `sbs download-cli` / `sbs self-update`, with sha256 verification against the manifest (§5.8) |

---

## 1. Goals & Non-Goals

### Goals

* A user sees **`sbs`** everywhere: usage, examples, errors, hints, config paths.
* Every command the CLI *suggests* is a command that **works when typed**.
* Data output stays byte-exact: `sbs list-skills | jq` is untouched, and a skill whose description contains the word "restish" is never rewritten (with this design, nothing rewrites anything).
* A **single-file native executable** per platform (`linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`, `windows-amd64`) downloadable from a running store: no Python, no `pip`, no separate restish install, no unpacking at startup.
* The downloaded executable **talks to the store it came from** with zero configuration.
* The download API is **unauthenticated in every ACL mode**, usable from a browser, `curl`, and the CLI.
* Artifact preparation at server start is **idempotent**: unchanged `SBS_PUBLIC_URL` ⇒ no work.
* Startup time and readiness unaffected.
* **One CLI codebase.** After this change there is exactly one implementation of `sbs` — Go — and no path that shells out to restish or requires it on `PATH`.

### Non-Goals

* Not a reimplementation of restish's request pipeline. We embed it; spec-driven command generation, auth, output formatting, pagination and caching stay upstream's.
* Not code signing or notarization (§7.4, §12).
* Not a package-manager story (Homebrew, winget, apt). The server-served binary, the install script and the platform wheels are the vehicles.
* Not multi-version artifact hosting: a store serves the build matching its own spec.
* **Not PyInstaller, and not a second Python implementation.** The Python shim is deleted (§4.6); §10.1 records why freezing a PyInstaller variant of it was rejected, and it is not a fallback, a phase, or an option to revisit.
* Not a fork of restish. Every branding hook used here is a documented public API; the two strings that have no hook get an upstream PR (§3.3).

---

## 2. What exists today

`sbs` is a ~390-line Python shim that shells out to restish:

| Fact | Where |
| --- | --- |
| Generation template with `{{API_NAME}}` / `{{API_URL}}` placeholders | [sdk_cli.py:22-23](../../skillberry-common/scripts/sdk_cli.py#L22-L23) |
| `make generate-sdk` substitutes them into the SDK | [dev.mk:209-233](../../skillberry-common/.mk/dev.mk#L209-L233) |
| `login`, `logout`, `connect` intercepted locally; everything else delegated | [sdk_cli.py:347-383](../../skillberry-common/scripts/sdk_cli.py#L347-L383) |
| Delegation is `os.execvp("restish", ["restish", "sbs", *argv])` — the shim **replaces itself**, so it never sees restish's output | [sdk_cli.py:381](../../skillberry-common/scripts/sdk_cli.py#L381) |
| A missing restish aborts with install instructions | [sdk_cli.py:58-67](../../skillberry-common/scripts/sdk_cli.py#L58-L67) |

Captured from `sbs --help`, `sbs get-skill --help` and `sbs bogus` (restish 2.3.0), the word reaches the user in at least these shapes:

```
Auth: run "restish api auth inspect sbs" for credential coverage.        # stdout, help
Usage:
  restish sbs [flags]                                                    # stdout, help
Examples:
  restish sbs list-vmcp-servers                                          # stdout, help
      --help-all            Show all inherited Restish flags in help     # stdout, help
Use "restish sbs --help-all" to show every Restish global flag.           # stdout, help
Use "restish sbs [command] --help" for more information about a command.  # stdout, help
unknown command "bogus" for "restish sbs"; run "restish sbs --help" …     # stderr, error
      --rsh-config string   Path to the restish config file …             # stdout, help
```

Two properties of the subprocess model are what made Feature A expensive before the pivot: the shim cannot see any of that text, and the command surface is inherently two levels deep (`restish sbs list-skills`), so even a renamed binary would print `sbs sbs list-skills`.

---

## 3. The pivot: restish as a Go library

### 3.1 What the embedding API provides

`restish.New()` returns a CLI whose branding and surface are ours to set. The methods used here — all exported from the root package, all documented as a long-term public promise:

| Call | Effect |
| --- | --- |
| `SetCommandName("sbs")` | The cobra root's `Use`, so **every** usage line, error and hint says `sbs` ([root.go:25-38](https://github.com/rest-sh/restish/blob/v2.3.0/internal/cli/root.go#L25-L38)) |
| `SetCommandDescription(short, long)` | Replaces restish's own `**Restish** is a CLI for…` blurb — and is where our "Skillberry Store commands" help section lives, as configuration rather than injected text |
| `SetVersion` | `sbs --version` / `sbs version` report our version, not restish's |
| `SetDefaultConfig(&restish.Config{APIs: …})` | Compiles in the base URL **and** spec URL. Defaults merge *under* user config, so `sbs connect` still wins — the exact precedence the previous design hand-rolled |
| `SetCommandSurface{PromotedAPI: "store"}` | Promotes generated operations to the **root**: `sbs list-skills`, not `sbs sbs list-skills` |
| `SetCommandSurface{SupportCommandNamespace, HideSupportCommands}` | Moves or hides `auth`/`cache`/`config`/`doctor`/`completion`/`version` |
| `AddAuthHandler(name, handler)` | Registers our standalone-auth scheme; `auth.Handler` gets a `Prompter`, a `TokenStore`, `Stderr` and a `Force` flag (§4.3) |
| `RSH_CONFIG_DIR` / `RSH_CACHE_DIR` / `RSH_CONFIG` env | Relocate config dir, cache dir and config file, so printed paths say `sbs` ([config/paths.go](https://github.com/rest-sh/restish/blob/v2.3.0/config/paths.go)) |

Upstream's own design record states the intent in the same terms this feature needs: *"a binary such as `acme` should feel like the Acme API CLI, not like generic Restish with a different executable name"*, and *"support commands at root must use the branded command name and product language … They should not say 'Restish'."*

### 3.2 Measurements

A 34-line `main.go` prototype was built and run against a live local store (restish v2.3.0, Go 1.26.8, Linux x86-64):

| # | Experiment | Result |
| --- | --- | --- |
| M1 | `sbs --help` with `PromotedAPI` | Root shows `sbs [flags]`, `sbs list-skills …`, `Manage local sbs configuration`, `Diagnose sbs configuration and runtime paths`, `Print the sbs version` |
| M2 | `sbs list-skills` with an **empty `HOME`**, no config file, URL baked via `-ldflags` | `[]` — worked with no `connect`, no config, no prompt |
| M3 | "restish" mentions across `--help`, operation help, `list-skills`, `version`, `doctor`, an unknown command, `config path`, `auth inspect` | **4 distinct strings** (§3.3) — down from the ~10 shapes in §2 |
| M4 | Cross-compile, `CGO_ENABLED=0`, from one Linux box | **all 5 targets built**; `file` confirms `Mach-O 64-bit arm64 executable` and `PE32+ executable for MS Windows` |
| M5 | Cold build per target | linux/amd64 0.5 s (warm cache) · linux/arm64 24.1 s · darwin/amd64 24.0 s · darwin/arm64 24.5 s · windows/amd64 32.2 s |
| M6 | **Re-link with a different baked URL** (`-ldflags -X`) | **2.3 s per platform** → ~12 s for all five |
| M7 | Artifact size (`-s -w`) | 30.1–33.1 MB, single static file, no extraction step |
| M8 | Baked URL present verbatim in the binary | yes (3 copies) — Go does not compress rodata, unlike a PyInstaller PYZ |
| M9 | **In-place slot patching**: 60-byte padded slot rewritten in a CI-built binary, no relink | 3 slots rewritten, **0 size delta**; the patched binary reached the store (`[]`, exit 0) from a clean `HOME`. Control build pointed at a dead port failed with a clear error, which is what makes M9 meaningful |

M4+M6 are why this design can do what the PyInstaller one could not: **prepare a per-URL artifact for every platform, at server start**. M9 is why it can do it even with no Go toolchain in the image.

### 3.3 The residual "restish" strings — complete inventory

| String | Where it shows | Resolution |
| --- | --- | --- |
| `--help-all  Show all inherited Restish flags in help` | every help screen | No public hook. **Upstream PR** (their design record already requires app vocabulary); until merged, accept — it is one flag description, and the flag itself is honest |
| `--rsh-config  Path to the restish config file …` | root help | Same; the flag *name* stays (`rsh` is not the word) |
| `doctor`: `Restish version:` label, and `run \`restish shell setup bash\`` | `sbs doctor` | The hint is also **wrong advice** (no `shell` command exists in a promoted surface). Set `SupportCommandNamespace: "cli"` so it is `sbs cli doctor`, and send the same upstream PR; `HideSupportCommands` removes it entirely if we prefer |
| Config **filename** `restish.json` (in `doctor` and `config path`) | both | Set `RSH_CONFIG=<config dir>/sbs.json`; the *directories* are already branded by `RSH_CONFIG_DIR` / `RSH_CACHE_DIR` (verified: `~/.config/sbs`, `~/.cache/sbs`, `~/.cache/sbs/specs`) |

So the worst case, with zero upstream changes, is **two flag descriptions in help**. Compare §10.1: the superseded design needed a stream rewriter, a protected-span list and a reserved-command passthrough to reach a weaker result.

### 3.4 Gotchas found while prototyping

1. **`-ldflags -X` silently no-ops unless the target variable has a constant string initializer.** The first prototype used `var urlSlot = "…" + strings.Repeat("#", 0)`; the flag was ignored and the binary fell back to its source default. It looked like it worked. What caught it was a control build pointed at a dead port that *should* have failed and didn't — so the implementation must keep that control as a test (§8.2 #12), not just eyeball the happy path.
2. **A promoted API fetches its spec when help or dispatch needs command metadata.** With an unreachable `SpecURL` the root help fails hard (`generated commands for promoted API "store" are unavailable: spec discovery failed …`) rather than degrading. That is upstream's documented behaviour for promoted roots, and it means the artifact must ship pointing at a URL that will resolve — plus a friendly wrapper for the offline case (§4.7 G3).
3. **`SetCommandDescription(short, "")` silently falls back to restish's own `**Restish** is a CLI for…` blurb.** Found while probing the FFI variant (§10.2): an empty long description reverted the branded root help. The long text must be non-empty, and §8.1 #6 asserts the root long help is ours.
4. The fetched spec is cached under `RSH_CACHE_DIR`, so the first invocation pays the fetch and later ones do not; `sbs doctor` reports freshness.

---

## Part A — A surface that only says `sbs`

## 4. Feature A design

### 4.1 The whole of it

```go
// client/go/cli/branding.go  (new; the entire branding layer)
func main() {
	brandPaths()                       // RSH_CONFIG_DIR / RSH_CACHE_DIR / RSH_CONFIG -> ~/.config/sbs, ~/.cache/sbs
	if handled, code := local(os.Args); handled { os.Exit(code) }   // connect | download-cli | self-update | version

	c := restish.New()
	c.SetCommandName("sbs")
	c.SetCommandDescription("Skillberry Store CLI", longHelp)       // includes our own command section
	c.SetVersion(version)                                           // -ldflags -X
	c.SetDefaultConfig(&restish.Config{APIs: map[string]*restish.APIConfig{
		"store": {BaseURL: baseURL(), SpecURL: baseURL() + "/openapi.json",
		          Profiles: map[string]*restish.ProfileConfig{ /* env:SBS_TOKEN */ }},
	}})
	c.SetCommandSurface(restish.CommandSurface{PromotedAPI: "store", SupportCommandNamespace: "cli"})
	c.AddAuthHandler("sbs-standalone", &standaloneAuth{})            // §4.3
	if err := c.Run(os.Args); err != nil { … }
}
```

`baseURL()` resolves, in order: `SBS_URL` env → the patchable slot / `-ldflags` value → the compiled-in default. User config (`sbs connect`) overrides all of it, because `SetDefaultConfig` merges *under* user config — so precedence is a library property, not something we re-implement and test.

### 4.2 Command surface

Decided: `PromotedAPI: "store"` with `SupportCommandNamespace: "cli"`.

* Operations at the root — `sbs list-skills`, `sbs get-skill x`, `sbs execute-tool y` — which is the whole ergonomic point.
* Support commands under one branded namespace: `sbs cli doctor`, `sbs cli cache clear`, `sbs cli config path`, `sbs cli auth inspect`. They stay available (support tickets, CI, recovery) but out of the primary help, which also keeps the residual strings of §3.3 off the first screen.
* `--version` keeps working regardless of where `version` lands (upstream guarantees this).
* Collisions are a **startup error** upstream: if an operation is ever named `cli`, `auth`, `cache` or `doctor`, the binary refuses to start with an actionable message. Mirrored by a repo test over `x-cli-name` (§8.1 #5) so we learn at PR time, not at release time.

### 4.3 Auth: a handler instead of an intercepted command

`auth.Handler` is public and receives everything the shim hand-rolled: `Prompter` (with `PromptSecret`), `TokenStore` (`Get`/`Set`/`Delete` — restish's own token cache), `Stderr`, `HTTPClient`, `BaseURL`, and `Force` (set after a 401 so a retry re-authenticates).

`standaloneAuth.Authenticate` therefore does what [sdk_cli.py:248-310](../../skillberry-common/scripts/sdk_cli.py#L248-L310) does today, minus the plumbing:

1. Cached token present and unexpired → attach `Authorization: Bearer …` and return.
2. Otherwise probe `GET /auth/whoami` once — the same two facts as `_preflight`: a `503 auth_disabled` means *attach nothing and proceed* (mode `disabled`), and a `401` may carry the operator's `login_info`, printed once to `ac.Stderr` before the first prompt (login-info.md §7 behaviour preserved exactly).
3. Prompt for username/password via `ac.Prompter`, `POST /auth/login`, store the token via `ac.TokenStore`.

Consequences worth stating: credentials never touch argv (the reason the shim fed restish through stdin), tokens live in restish's token cache with its permissions handling rather than in a config file we chmod ourselves, and **any** command triggers login when needed — so `sbs list-skills` on a fresh install just works. `sbs login` / `sbs logout` survive as thin verbs (pre-authenticate now; drop the cached token) because users expect them, not because the flow needs them.

`SBS_TOKEN` stays supported through the baked profile's `token: env:SBS_TOKEN`, which is how the shim's `env-token` profile worked — now a literal in our default config instead of a subprocess call.

### 4.4 Paths

`brandPaths()` sets, unless the user already did:

```
RSH_CONFIG_DIR = ~/.config/sbs      RSH_CONFIG = ~/.config/sbs/sbs.json      RSH_CACHE_DIR = ~/.cache/sbs
```

Verified in the prototype: `doctor` then reports `~/.config/sbs`, `~/.cache/sbs`, `~/.cache/sbs/specs`, `~/.config/sbs/tokens.cbor`.

Migration for existing users, once: if `~/.config/sbs/sbs.json` is absent and `~/.config/restish/restish.json` exists, copy **only** the `apis.sbs` entry (base URL + stored bearer), `0600`, and print one stderr line. Nothing is deleted, so a user who also drives restish directly is unaffected.

### 4.5 What happens to the Python shim's 390 lines

| Today | Fate in Go |
| --- | --- |
| `check_restish_installed`, `abort_with_install_instructions` (30 l) | **deleted** — the engine is linked in; there is nothing to find on `PATH` |
| `_strip_jsonc`, `_config_path`, `_load_config`, `_write_config` (55 l) | **deleted** — the `config` package owns the file, its JSONC comments and its permissions |
| `_registered_base` (shells out to `api list -o json`, parses) (20 l) | **deleted** — `cli.Config()` is typed access |
| `_restish_connect` (shells out to `api connect`) (16 l) | **deleted** — `SetDefaultConfig` supplies base + spec; there is no first-run connect step |
| `_ensure_env_profile` (16 l) | **deleted** — `token: env:SBS_TOKEN` is a literal in the baked default config |
| `_store_bearer` (13 l) | **replaced** — `auth.TokenStore` |
| `_preflight` (41 l) | **ported**, ~30 lines, inside the auth handler (§4.3) |
| `_do_login`, `_do_logout` (85 l) | **absorbed** by the handler; ~40 lines of thin verbs remain |
| `_do_connect`, `_retry_args`, `cli()` + `execvp` (57 l) | ~30 lines: one config write, one profile default, one dispatch |
| Output scrubber, protected spans, reserved passthrough, help injection (~150 l, **planned** in §10.1) | **never written** |
| New: `download-cli`, `self-update` (§5.8) | ~80 lines |

Net ≈ **150–200 lines of Go** against 390 Python + 150 planned. The generated operations were never our code; they come from the spec either way.

### 4.6 Distribution: the Python CLI is deleted

**Decided: one codebase.** The Python shim is removed, not frozen and not kept as a fallback. Two implementations of one CLI would drift, and the shim is precisely the thing whose subprocess model created Feature A's problem — keeping it alive would keep that problem alive on one supported path.

Concretely, this feature **deletes**:

| Deleted | Note |
| --- | --- |
| `client/python/skillberry_store_sdk/skillberry_store_sdk/sdk_cli.py` | The generated shim |
| The CLI injection in `make generate-sdk` — template copy, `setup.py` `entry_points`, `[tool.poetry.scripts]` ([dev.mk:216-233](../../skillberry-common/.mk/dev.mk#L216-L233)) | Gated off for this asset by a new `SDK_PY_CLI := 0` in [.mk/local.mk](../../.mk/local.mk); the flag defaults to `1` in `dev.mk` so **sibling assets that still use the shim are unaffected** (the template lives in a shared subtree — §4.7 G6) |
| `src/skillberry_store/tests/cli/test_sdk_cli_login_info.py` | Its guarantees move to Go tests on the auth handler (§8.1 #4), so the `login_info` contract of [login-info.md](login-info.md) §7 stays covered |
| Every "install restish" instruction in `docs/cli.md`, `site/cli.html`, `README.md` | They describe a prerequisite that no longer exists |

and **adds** one distribution package:

* **`skillberry-store-cli`** — platform wheels (`py3-none-manylinux_2_17_x86_64`, `…-macosx_11_0_arm64`, `…-win_amd64`, …) carrying the Go binary as package data plus a three-line launcher that `os.execv`s it. Same pattern as `ruff`, `uv` and `esbuild`.
* The generated OpenAPI **SDK stays pure Python and universal** — it is a library and must install anywhere. It simply stops declaring an `sbs` console script.
* `skillberry-store-sdk[cli]` depends on `skillberry-store-cli` behind environment markers, so the documented install line stays close to what users type today.
* On a platform with no wheel, `pip` gets the SDK and no `sbs`; the CLI comes from the store itself (`/cli/install.sh`, or the UI download). That is a better fallback than a Python shim, because it is the same binary everyone else runs.

### 4.7 Issues & resolutions — Feature A

| # | Issue | Resolution |
| --- | --- | --- |
| G1 | Two flag descriptions still say "Restish" and have no public hook | Upstream PR aligned with upstream's own accepted requirement; meanwhile accept. Worst case is two lines in help — strictly better than today and than §10 (§3.3) |
| G2 | `doctor`'s `restish shell setup` hint is wrong advice in a promoted surface | `SupportCommandNamespace: "cli"` moves it off the primary surface; upstream PR fixes the text; `HideSupportCommands` is the blunt option (§4.2) |
| G3 | A promoted API **fails hard** when its spec is unreachable, so `sbs --help` offline is an error, not degraded help | Wrap `Run`'s error: on a spec-discovery failure, print one branded line naming the configured URL and the three ways to change it (`sbs connect`, `SBS_URL`, re-download). Cached specs mean this only bites a first run against an unreachable store (§3.4 #2) |
| G4 | We take a dependency on an API upstream calls "first pass" (no custom Go commands yet, no embedded-spec helper) | Everything used here is documented as a maintained public promise, and our extra verbs live in `main()` **before** `Run`, needing no upstream command registration. The version is pinned (`go.mod` + `go.sum`), so an upstream change is a deliberate upgrade |
| G5 | A Go CLI cannot be a `pip` console script | Platform wheels carrying the binary plus a launcher (§4.6). The wheel, the UI download and `sbs download-cli` all ship the *same* artifact |
| G6 | The Python shim's template lives in a **shared subtree** (`skillberry-common`), so deleting it outright could break sibling assets | Gate the generation step behind `SDK_PY_CLI` (default `1`), set `0` here. The template gains a deprecation note pointing at this design; a follow-up removes it from `skillberry-common` once no asset sets `SDK_PY_CLI=1` |
| G7 | **Breaking change**: `pip install skillberry-store-sdk` stops providing `sbs`, so an upgrade removes a command users have today | Deliberate and documented: a `BREAKING:` CHANGELOG entry naming the remedy (`pip install skillberry-store-cli`, or `skillberry-store-sdk[cli]`), which is what [test_changelog.py](../../src/skillberry_store/tests/test_changelog.py) exists to enforce (§9.1). No silent degradation: the SDK's last shim release keeps working for anyone who pins it |
| G8 | `-ldflags -X` silently ignores non-constant initializers | A control test that asserts a binary built against a dead port **fails**; caught a false positive during prototyping (§3.4 #1, §8.2 #12) |
| G9 | Existing users' config lives in `~/.config/restish` | One-time copy of the `apis.sbs` entry with a stderr notice; old file untouched (§4.4) |
| G10 | Go's toolchain becomes a build dependency for the CLI | CI builds it; the runtime image needs it only for the opt-in per-URL build path (§5.4). `pip`-only contributors are unaffected: `make generate-sdk` no longer produces a CLI, and building one is a release-time concern, not a prerequisite for working on the store |

---

## Part B — The CLI as a native download

## 5. Feature B design

### 5.1 Platforms and artifacts

| Platform id | Artifact | Notes |
| --- | --- | --- |
| `linux-amd64` | `sbs` | static, no glibc floor (`CGO_ENABLED=0`) |
| `linux-arm64` | `sbs` | |
| `darwin-amd64` | `sbs` | |
| `darwin-arm64` | `sbs` | Go's linker ad-hoc signs this target — relevant to §5.2 |
| `windows-amd64` | `sbs.exe` | |
| `windows-arm64` *(stretch)* | `sbs.exe` | trivial to add (`GOOS/GOARCH`); gated only on wanting to test it |

Ids are `<goos>-<goarch>`, a **closed enum** server-side (§7.2).

### 5.2 Preparing a per-URL artifact

Two mechanisms, both verified (M6, M9). The manifest records which one produced each artifact, so the UI, the CLI and support can see it:

> **As built:** `patch` for four platforms and `rebuild` only for `darwin-arm64`
> (sidecar when no toolchain is present) — the cheapest mechanism that works, per
> platform. See §11.1 R6.

| | `rebuild` | `patch` |
| --- | --- | --- |
| How | `GOOS/GOARCH go build -ldflags "-X <cli-pkg>.URLSlot=<url>"` (see the layout note in §9) | Rewrite the padded 60-byte slot in a CI-built artifact, in place, same length |
| Needs | Go toolchain + vendored deps in the image | nothing but the artifact |
| Cost | 2.3 s per platform (~12 s for five) | ~milliseconds |
| Verified | M4/M6 — all five targets | M9 — Linux, 0 size delta, runs |
| Caveat | none | changes bytes inside a signed image: `darwin-arm64` is ad-hoc signed by Go's linker, so a patch is expected to break exec. CI probes each platform and records the verdict |

Resulting per-platform plan — the uncertain cells are decided by a CI probe (§5.10 step 3), not by an assumption in this document:

| Platform | Toolchain present | No toolchain |
| --- | --- | --- |
| `linux-amd64`, `linux-arm64` | rebuild | **patch** (verified) |
| `windows-amd64` | rebuild | patch (PE tolerates it; probe confirms) |
| `darwin-amd64` | rebuild | patch if the probe passes, else sidecar |
| `darwin-arm64` | rebuild | **sidecar + install script** (patch expected to break the ad-hoc signature) |

The `sidecar` fallback is the previous design's mechanism, now needed for at most one platform in one configuration: the download archive carries `sbs` plus a one-line `sbs.url`, which the CLI reads on first run and folds into its config. The `curl | sh` install script covers the same case more smoothly and, on macOS, also avoids Gatekeeper quarantine (§5.7).

### 5.3 The stamp — "no work for the same URL"

`$SBS_CLI_DIST_DIR/manifest.json` is both the served document and the stamp. Per platform:

```
key = sha256( public_url ‖ cli_commit ‖ restish_version ‖ platform ‖ mechanism )
```

Preparation is skipped when the recorded key matches *and* the file exists with the recorded size and sha256. So work happens exactly on: a changed `SBS_PUBLIC_URL`, a new CLI commit, a restish upgrade, a missing or corrupt file. `SBS_CLI_PREPARE=always|auto|never` forces or disables.

Runs as a background task off the existing lifespan hook, beside the encoder warmup ([server.py:138-146](../../src/skillberry_store/fast_api/server.py#L138-L146)), in a thread executor with the build as a **subprocess**; failures are logged and downgrade the platform's `state`, never the server's. **`/health/ready` deliberately does not gate on it** ([admin_api.py:178-200](../../src/skillberry_store/fast_api/admin_api.py#L178-L200)): readiness means "can answer content requests".

Atomicity: a `fasteners` inter-process lock (already a dependency) around the dist dir, `*.tmp` + `os.replace`, manifest written last. The ETag is the content sha256, so two replicas that each prepared their own copy still agree. Storage is `SBS_CLI_DIST_DIR`, default `{SBS_BASE_DIR}/cli-dist` — a cache, not state; an ephemeral `/tmp` costs one background re-preparation.

### 5.4 The Go toolchain question

| Option | Image cost | What it buys |
| --- | --- | --- |
| **A — default: no toolchain.** Five CI-built artifacts with a padded slot, baked into the image (~160 MB) or lazily fetched (sha256-pinned) | ~160 MB, or 0 with egress | Per-URL artifacts for every platform except `darwin-arm64`, in milliseconds, with no compiler in production |
| **B — opt-in `-cli-builder` image variant.** Go toolchain + vendored deps | ~250 MB toolchain + vendored source (after fetching restish's dependency graph the Go module cache on the prototype machine measured **377 MB** — it keeps zips and every version, so vendoring plus `GOFLAGS=-mod=vendor` is what keeps this bounded; the vendored tree's size is a number to pin during implementation) | Real per-URL builds for all five, `darwin-arm64` included, no patching caveat |

Default **A**: it satisfies "prepare artifacts for the current `SBS_PUBLIC_URL` at server start, skip if already current" without shipping a compiler, and it is the only option for air-gapped images that also refuse build toolchains. **B** is one build arg away for operators who want the last platform covered. Either way the server-side contract — manifest, endpoints, stamp — is identical, which is what keeps this a deployment choice rather than a design fork.

### 5.5 API surface

#### 5.5.1 Endpoints

> **As built: one endpoint, not five.** `GET`/`HEAD` `/cli/download`, with an
> artifact's identity in response headers. See §11.1 R1–R3 for what was dropped
> and why. The rest of this subsection is the original proposal, kept for
> traceability.

New `fast_api/cli_api.py`, registered from `SBS.__init__`:

| Method | Path | Answer |
| --- | --- | --- |
| `GET` | `/cli/manifest` | `200` JSON always (per-platform `state`, even when nothing is ready) |
| `GET`, `HEAD` | `/cli/download` | `200` artifact; `400` unknown platform; `404` `not_bundled`; `503` + `Retry-After: 10` while preparing; `429` rate-limited |
| `GET` | `/cli/install.sh` | `200 text/x-shellscript`, generated, URL inlined |
| `GET` | `/cli/install.ps1` | `200 text/plain`, generated |
| `GET` | `/cli/license` | `200 text/plain` — restish's MIT licence, since we redistribute it |

Parameters: `platform` (enum; omitted ⇒ detected, §5.6) and `format` (`raw` \| `archive`; `archive` for browsers, `raw` otherwise).

```json
{
  "cli_name": "sbs", "cli_version": "0.1.0+g3e46b0d",
  "public_url": "https://store.example.com", "generated_at": "2026-09-24T09:12:03Z",
  "engine": {"name": "restish", "version": "2.3.0", "license": "MIT", "license_url": "/cli/license"},
  "platforms": {
    "linux-amd64": {"state": "ready", "filename": "sbs", "size": 32300000, "sha256": "…",
                    "url_injection": "patch", "download_url": "/cli/download?platform=linux-amd64&format=raw",
                    "archive_url": "/cli/download?platform=linux-amd64&format=archive", "archive_sha256": "…"},
    "darwin-arm64": {"state": "ready", "url_injection": "sidecar", "…": "…"},
    "windows-arm64": {"state": "unavailable", "reason": "not_bundled"}
  }
}
```

`state` ∈ `ready` \| `preparing` \| `unavailable`. Relative URLs, so the document is correct behind any prefix; one document feeds the UI, the CLI and the install scripts.

#### 5.5.2 Transport details that matter

* **`FileResponse`**, not `StreamingResponse` (contrast [admin_api.py:103](../../src/skillberry_store/fast_api/admin_api.py#L103)): Range requests, `Last-Modified` and conditional GETs come free, which is what makes a 32 MB download resumable and a repeat cheap.
* `ETag: "<sha256>"`, `Cache-Control: public, max-age=300`, `Vary: Sec-CH-UA-Platform, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, User-Agent` — without `Vary`, one shared cache hands a Windows user the Linux binary.
* `Content-Disposition: attachment; filename="sbs"` / `"sbs.exe"` / `"sbs-<platform>.tar.gz"` / `".zip"`.
* `format=archive` is the browser default for three real reasons: a browser download loses the executable bit, macOS stamps it `com.apple.quarantine`, and the `sidecar` mechanism needs a second file. The archive holds `sbs`, `LICENSE.restish`, and `sbs.url` where the mechanism says so.
* `HEAD` on the same route — the ACL audit requires every method on a route to be allow-listed, the reason `/ui` lists `HEAD` today ([config.py:249-254](../../src/skillberry_store/access_control/config.py#L249-L254)).

#### 5.5.3 Unauthenticated in every mode — and an existing footgun

"Independent of ACL mode" is not achieved by extending `_DEFAULT_UNAUTH_PATHS`, because the loader *replaces* the defaults with the operator's list:

```python
unauth_paths = list(raw.get("unauthenticated_paths") or _DEFAULT_UNAUTH_PATHS)   # config.py:361
```

An operator with their own list (as `access_control_config.yaml.standalone` has) would authenticate `/cli/*` — and already loses `/health`, `/openapi.json` and `/docs` unless they re-listed them. One change fixes both:

```python
_ALWAYS_UNAUTH_PATHS = [                 # floor, merged in regardless of the config file
    "GET /health", "GET /health/ready",
    "POST /auth/login", "POST /auth/logout", "GET /auth/whoami",
    "GET /cli*", "HEAD /cli*",
]
unauth_paths = _merge(_ALWAYS_UNAUTH_PATHS, raw.get("unauthenticated_paths") or _DEFAULT_UNAUTH_PATHS)
```

with a boot log line naming anything the floor added that the file omitted, so the widening is never silent. The shipped YAMLs list `GET /cli*` / `HEAD /cli*` with a comment, so the file stays a complete description of the public surface. The RBAC audit ([audit.py:200](../../src/skillberry_store/access_control/audit.py#L200)) is then satisfied with **no `@requires`** on these routes, matching `/health` and `/admin/metrics`.

#### 5.5.4 Spec, SDK and MCP

`openapi_extra={"x-cli-name": "cli-manifest"}` and `"download-cli"`; **no `x-mcp-tool`** (a 32 MB octet-stream is not an agent tool, and the curated-surface audit is where that choice is recorded). The install scripts and `/cli/license` are `include_in_schema=False`. New endpoints oblige a `make generate-sdk` + `docs/cli.md` + `site/cli.html` refresh in the same PR (§9).

### 5.6 Platform detection

| # | Source | Notes |
| --- | --- | --- |
| 1 | `?platform=` | Always wins: what the UI sends, what scripts pin, what a bug report reproduces |
| 2 | `Sec-CH-UA-Platform` + `-Arch` + `-Bitness` | Chromium only, and the high-entropy hints arrive **only after** the origin advertises them, so the `/ui` handler ([server.py:484-530](../../src/skillberry_store/fast_api/server.py#L484-L530)) gains `Accept-CH` + `Critical-CH` for the trio |
| 3 | `User-Agent` | Windows/macOS/Linux, `x86_64`/`aarch64` on Linux. **Cannot** distinguish Apple Silicon from Intel — every Mac reports `Intel Mac OS X 10_15_7` |
| 4 | Default `linux-amd64` | plus `X-SBS-Platform-Detection: default`, so a wrong guess is diagnosable |

Because of #3 the UI resolves the arch with `navigator.userAgentData.getHighEntropyValues(['architecture','bitness'])` when available and **always** shows a chooser with the detection preselected. `curl` users get an exact answer from the install script's `uname -sm`. A bare `curl /cli/download` from a Mac gets `darwin-arm64` with the "guessed" header.

### 5.7 Install scripts

```bash
curl -fsSL https://store.example.com/cli/install.sh | sh
```

Generated per request with the URL and expected sha256 inlined: detect `uname -sm`, download `format=raw`, verify sha256, install to `~/.local/bin/sbs` (`$SBS_INSTALL_DIR` override), `chmod +x`, write the sidecar when the manifest says the platform needs one, print the next command. `install.ps1` is the Windows twin. On macOS this is the *best* path, not a consolation: `curl`-fetched files carry no quarantine attribute.

Security requirement, sharp: the URL inlined into a generated shell script must be **validated, not interpolated**. From `SBS_PUBLIC_URL` it was normalised at startup; derived from the request it comes from a client-controlled `Host`, and a value like `evil.com/"$(id)"` inside a script is command injection on the user's machine. The generator accepts only `^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$`, single-quotes it, and otherwise refuses to emit (`503`, reason logged). **The same validation gates `-ldflags`** (§7.2): an unvalidated URL reaching a linker flag is argument injection into our own build.

### 5.8 The CLI downloading the CLI

`sbs download-cli [--platform <id>] [--output <path>] [--format raw|archive]` and `sbs self-update`, handled in `main()` before `Run` (no upstream command registration needed — §G4):

1. `GET /cli/manifest`, default to the running platform (`runtime.GOOS`/`runtime.GOARCH` — exact, unlike a browser).
2. Refuse politely on `preparing` (with the `Retry-After` hint) and `unavailable` (with the reason).
3. Stream to a temp file beside the target, **verify sha256 against the manifest**, `chmod 0755`, `os.Rename`.
4. `self-update` replaces the running binary; on Windows, where that is not permitted, write `sbs.exe.new` and print the one-line rename.

This is also the end-to-end test the requirement asks for (§8.3 #22).

### 5.9 UI

One component, two entry points:

* `components/CliDownloadModal.tsx` — PatternFly v5 `Modal`: detected platform preselected, a `FormSelect` with unavailable platforms disabled and labelled, a primary **Download** as a plain `<a href download>` (the browser owns a 32 MB transfer better than a blob), sha256 in a `ClipboardCopy`, the `curl | sh` one-liner in a second `ClipboardCopy`, engine licence link.
* `AppLayout.tsx` masthead: a `DownloadIcon` button with `aria-label="Download CLI"` beside `UserBadge` ([AppLayout.tsx:84-88](../../src/skillberry_store/ui/src/components/AppLayout.tsx#L84-L88)).
* `HomePage.tsx`: a fifth card, "CLI", opening the same modal.
* **`LoginPage.tsx`: a `DownloadIcon` button under the sign-in card**, icon-only with `aria-label="Download CLI"` to match the masthead, opening the same modal. Icon-only because the masthead control is the only one that survives sign-in and `mode: disabled`, so a caption here would make the same affordance look like two. **Decided: yes**, because a user who cannot yet sign in is exactly the user who wants the CLI, and the endpoints are unauthenticated by construction (§5.5.3), so the modal works pre-session with no special case. It renders below `CardBody` ([LoginPage.tsx:82-154](../../src/skillberry_store/ui/src/pages/LoginPage.tsx#L82-L154)) and beneath any `LoginBanner`, so an operator's message stays the first thing read. In `mode: disabled` there is no login screen and nothing changes.

States: `preparing` → spinner and "being prepared, try again shortly"; all-unavailable → explain, and offer the routes that need no prepared artifact (`pip install skillberry-store-cli`, or the install-script one-liner); fetch failure → inline `Alert`, never a blank modal. `services/api.ts` gains `getCliManifest()` and the types.

### 5.10 CI

New `.github/workflows/cli-artifacts.yml`. Because Go cross-compiles, **one** `ubuntu-latest` job builds all five artifacts (contrast §10, which needed five OS runners), and the platform-specific runners are used only for what genuinely needs the OS — executing the artifact:

1. `ubuntu-latest`: build all five with the padded slot (`CGO_ENABLED=0`, `-trimpath`, pinned restish via `go.mod`/`go.sum`), emit `prebuilt-manifest.json` with sha256 per artifact.
2. Matrix over `ubuntu-latest`, `ubuntu-24.04-arm`, `macos-15-intel`, `macos-14`, `windows-latest`: run that platform's artifact — `sbs --version`, `sbs --help`, and **assert no `restish`/`Restish` beyond the known allowlist of §3.3** (the regression gate for Feature A, on the real binary, on every OS).
3. **Mechanism probe** per platform: patch the slot, run the patched binary, assert it reports the injected URL → records `patch` or `sidecar` in the manifest (§5.2).
4. Smoke against a store started in the job: `sbs list-skills`, `sbs download-cli`, then run the downloaded binary.
5. Publish artifacts + manifest as release assets; the image build consumes the same manifest.

### 5.11 Issues & resolutions — Feature B

| # | Issue | Resolution |
| --- | --- | --- |
| B1 | Cross-platform builds | Solved by construction: Go, `CGO_ENABLED=0`, verified for all five from one Linux box (M4) |
| B2 | Baking `SBS_PUBLIC_URL` without a toolchain | In-place slot patching, verified (M9); `rebuild` when a toolchain is present (M6) |
| B3 | `darwin-arm64` is ad-hoc signed by Go's linker, so patching should break exec | Sidecar + install script for that one cell in the no-toolchain configuration; option **B** covers it with a real build. CI probe decides rather than this document (§5.2) |
| B4 | Prebuilt artifacts add ~160 MB to the image | Build arg: bake all five (release images), bake none and fetch lazily with sha256 pinning, or run option **B**. `unavailable` with a reason is a truthful gap (§5.4) |
| B5 | A Go toolchain in the runtime image is a posture change ("compiler in production") | Not the default; when enabled: vendored deps, `GOFLAGS=-mod=vendor`, `GOPROXY=off`, `GOCACHE` under `SBS_BASE_DIR`, and no client input in the build (§7.2) |
| B6 | Ephemeral `/tmp` ⇒ re-preparation on cold start | It is a cache; patching is milliseconds and a rebuild is 2.3 s per platform in the background. `SBS_CLI_DIST_DIR` can point at a volume (§5.3) |
| B7 | Two workers/replicas could serve a half-written artifact | Inter-process lock, `os.replace`, manifest last; content-hash ETag so independent copies agree (§5.3) |
| B8 | An operator's `unauthenticated_paths` **replaces** the defaults, so `/cli/*` (and today `/health`) would demand auth | `_ALWAYS_UNAUTH_PATHS` floor, merged in every mode, logged (§5.5.3) |
| B9 | Apple Silicon vs Intel is undecidable from `User-Agent` | Client Hints, `navigator.userAgentData`, `uname -sm`, an always-visible chooser, and a header marking a guess (§5.6) |
| B10 | `Arch`/`Bitness` hints need an advertisement first | `/ui` responses carry `Accept-CH` + `Critical-CH` (§5.6) |
| B11 | A shared cache could serve the wrong platform | `Vary` on the detection inputs (§5.5.2) |
| B12 | An unauthenticated 32 MB download is free bandwidth | Per-IP token bucket, concurrency cap, `SBS_CLI_DOWNLOAD=off`, ETag/`304`, `max-age`; ingress/CDN caching recommended (§7.3) |
| B13 | `platform` as a traversal vector | Closed enum → a table of prepared paths; the value never reaches a path join (§7.2) |
| B14 | A spoofed `Host` could bake an attacker's URL, and a raw `Host` in a generated script or an `-ldflags` value is injection | Only `SBS_PUBLIC_URL` feeds `rebuild`/`patch`; strict validation + quoting before a script, argv-only exec for the linker (§5.7, §7.2) |
| B15 | Spec drift between a downloaded CLI and the store | restish refreshes the spec from the server, so operations track the store; the manifest carries `cli_version` and `sbs version` warns when the store advertises a newer build |
| B16 | A newly downloaded binary keeps an older `sbs connect` registration | Auto-registration is stamped; a changed baked URL re-points only auto entries, with a notice. A user's explicit `connect` is never overridden (§4.1) |
| B17 | macOS quarantine, Windows SmartScreen, AV false positives on unsigned binaries | Documented in `docs/cli.md` with the `xattr -dr com.apple.quarantine` remedy and the `curl \| sh` path recommended for macOS. A Go binary attracts fewer AV false positives than a PyInstaller one, but it is still unsigned; signing is §12 |
| B18 | The store becomes a software distribution channel | sha256 in the manifest, in the UI, verified by the install script and `download-cli`; `-trimpath` and a pinned dependency graph make builds reproducible; SLSA provenance in §12 (§7.4) |

---

## 6. Configuration surface

> **As built: one variable, not eight.** `SBS_CLI_DOWNLOAD` gates the feature and
> everything else is derived. See §11.1 R5. The table below is the original
> proposal, kept for traceability.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SBS_PUBLIC_URL` | unset | Externally reachable base URL. **Adopted from [npx.md §5.11](npx.md) unchanged**: `Field(None, validation_alias="SBS_PUBLIC_URL")` on `SBSettings` ([server.py:58-68](../../src/skillberry_store/fast_api/server.py#L58-L68)), scheme required, trailing slash stripped, logged at boot. Precedence: `SBS_PUBLIC_URL` → `request.base_url` (manifest links and install scripts only, never a baked artifact) → warn and serve pristine |
| `SBS_CLI_DOWNLOAD` | `on` | `off` unregisters the endpoints entirely |
| `SBS_CLI_PREPARE` | `auto` | `auto` \| `always` \| `never` — per-URL preparation at start |
| `SBS_CLI_BUILD_MODE` | `patch` | `patch` (no toolchain) \| `rebuild` (option **B**) \| `auto` (rebuild where a toolchain exists) |
| `SBS_CLI_DIST_DIR` | `{SBS_BASE_DIR}/cli-dist` | Prepared artifacts + `manifest.json` (a cache) |
| `SBS_CLI_ARTIFACTS_DIR` | `/app/cli-prebuilt` | CI artifacts baked into the image or mounted |
| `SBS_CLI_ARTIFACTS_URL` | unset | Base URL for lazy, sha256-pinned fetching of missing platforms |
| `SBS_CLI_MAX_CONCURRENT_DOWNLOADS` | `8` | Crude backpressure (§7.3) |
| `SBS_URL` *(client)* | unset | Overrides the CLI's default host |
| `SBS_TOKEN` *(client)* | unset | Bearer for CI/scripting, via the baked `env:` profile (§4.3) |

All of these belong in [docs/config-env-vars.md](../config-env-vars.md), and the deployment defaults in [container.env](../../container.env).

---

## 7. Security review

### 7.1 What the new surface exposes

The store's own public URL and the CLI's version — both already public in practice. No tenant data, no config, no login message, no request body accepted. Worth stating plainly because this is the store's first knowingly-unauthenticated binary surface: the threat model is availability and supply chain, not confidentiality.

### 7.2 Input handling

`platform` and `format` are closed enums resolved through a dict of prepared artifacts; no client string is joined onto a path (the lesson `/ui` already encodes, [server.py:496-499](../../src/skillberry_store/fast_api/server.py#L496-L499), guarded by `test_path_traversal_fix.py`). A client-derived URL never reaches a shell, a linker flag, or a patched artifact without passing the §5.7 validator; builds are invoked as an argv list, never through a shell; the patched slot is length-checked against the slot size and `#`-padded, and the sha256 is recomputed **after** patching.

### 7.3 Availability

A 32 MB artifact behind an unauthenticated GET is an amplification opportunity. Defence in depth: per-IP token bucket and concurrency cap in-process (honestly per-replica), `ETag`/`304` + `max-age=300` so repeats are nearly free, `Retry-After` instead of queueing while preparing, and `SBS_CLI_DOWNLOAD=off`. Operators should let the ingress or a CDN cache `/cli/*`; the response is immutable for a given ETag.

### 7.4 Supply chain

We hand users an executable, so: sha256 in the manifest, shown in the UI, verified by the install script and `download-cli`; restish pinned by `go.mod`/`go.sum` and vendored for reproducibility; `-trimpath` so paths do not leak and builds are comparable; its MIT licence served at `/cli/license` and shipped in every archive; artifacts built in CI from a tagged commit. The binaries are **unsigned** — macOS quarantines a browser download and SmartScreen warns — stated in the docs rather than worked around. Signing, notarization and provenance are §12.

---

## 8. Test plan

### 8.1 Feature A — Go unit tests (`client/go/tests/*_test.go`)

1. `brandPaths` sets the three env vars, respects a user-set value, and produces branded paths.
2. Config migration copies only `apis.sbs`, sets `0600`, leaves the old file, prints one line.
3. `baseURL()` precedence: user config > `SBS_URL` > slot/ldflags > compiled default.
4. `standaloneAuth`: cached token reused; `503 auth_disabled` → no header and no prompt; `401` with `login_info` → message to stderr **once**, before the first prompt; successful login stores the token; `Force` re-authenticates. These are the assertions inherited from the deleted `test_sdk_cli_login_info.py`, so the [login-info.md](login-info.md) §7 contract keeps a test after the Python shim is gone.
5. Reserved-name guard (pytest, repo side): no `x-cli-name` in the OpenAPI spec collides with `cli`, `auth`, `cache`, `config`, `doctor`, `completion`, `version`, or a local verb — upstream makes such a collision a startup failure, and this catches it at PR time.
6. Help assertions on a built binary: root usage says `sbs`, **the root long description is ours** (guards §3.4 #3), operations are at the root, and no `restish` appears outside the §3.3 allowlist (which is itself asserted to be exactly four entries, so an upstream fix or regression is visible).

### 8.2 Feature B — server (pytest)

7. Manifest shape and states (`ready`/`preparing`/`unavailable` with reasons).
8. Stamp: unchanged URL ⇒ zero preparation calls; changed URL / commit / restish version / corrupt file ⇒ re-preparation.
9. Patch mechanism: slot rewritten, **size unchanged**, recorded sha256 is the post-patch one; an over-long URL is rejected before writing.
10. `503 + Retry-After` while preparing; `404 not_bundled`; `400` unknown platform; `429` past the cap.
11. Detection matrix: explicit param; Chromium hint trios; Safari/Firefox macOS UA → `darwin-arm64` + guessed header; Windows UA; unknown → default; `Vary` on every answer; `/ui` carries `Accept-CH` + `Critical-CH`.
12. **`-ldflags` control** (option **B** only): a binary built against a dead port must fail; guards the silent-no-op gotcha of §3.4 #1.
13. `HEAD` returns the same headers and no body; a Range request returns `206`.
14. Traversal: `platform=../../etc/passwd` → `400`, nothing read.
15. ACL floor: with an operator config whose `unauthenticated_paths` omits everything, `/cli/manifest` and `/cli/download` stay reachable in `standalone` mode, the boot log names the widening, and the RBAC audit still passes with no `@requires`.
16. Install-script generation: URL inlined and quoted; a hostile `Host` (`evil.com/"$(id)"`) yields **no script** and a logged refusal; the same validator rejects it as an `-ldflags` value.
17. Readiness is independent: with preparation pending, `/health/ready` is `200`.

### 8.3 End to end and UI

18. CI matrix (§5.10): each platform runs its own artifact; help contains no unexpected `restish`; the mechanism probe records `patch` or `sidecar`.
19. `sbs list-skills` against a store from a clean `HOME` with no config file returns data — the zero-config property, which is M2 turned into a test.
20. `sbs download-cli` → execute the downloaded artifact → `sbs list-skills` against the same store, unconfigured.
21. Vitest: modal preselects the detected platform, disables unavailable ones, shows sha256, handles `preparing` and fetch failure; the download control is an `<a href>` with the right query string; the masthead button has an accessible label.
21b. Vitest, login screen: the download button renders **unauthenticated** and icon-only (no visible caption), sits below any `LoginBanner`, opens the modal, and the modal's manifest fetch carries no credentials — i.e. it works in `standalone` mode before a session exists.
21c. Packaging: the generated SDK declares **no** `sbs` console script (neither `setup.py` `entry_points` nor `[tool.poetry.scripts]`), and `sdk_cli.py` is absent — the regression test for the deletion.
22. Docs currency, in the style of `test_ui_docs_current.py`: `docs/cli.md` and `site/cli.html` describe the native download and no longer tell users to install restish by hand.

---

## 9. Files touched, and the order to do it in

| Area | Files |
| --- | --- |
| The CLI | **new** `client/go/go.mod`, `go.sum`, `build.sh`; `client/go/cli/` (`branding.go`, `auth.go`, `dist.go`, `login.go`, `paths.go`, `verbs.go`, `version.go`); `client/go/cli/cmd/sbs/main.go`; `client/go/tests/*_test.go`. **Layout note** below |
| **Deletions** (§4.6) | `client/python/skillberry_store_sdk/skillberry_store_sdk/sdk_cli.py`; `src/skillberry_store/tests/cli/test_sdk_cli_login_info.py`; the CLI injection in [dev.mk:216-233](../../skillberry-common/.mk/dev.mk#L216-L233), gated by a new `SDK_PY_CLI` flag set to `0` in [.mk/local.mk](../../.mk/local.mk); `skillberry-common/scripts/sdk_cli.py` gains a deprecation note and is removed in a follow-up once no sibling asset opts in |
| Packaging | **new** `skillberry-store-cli` platform wheels (binary + launcher); `skillberry-store-sdk[cli]` extra with environment markers |
| Server | **new** `fast_api/cli_api.py`, `fast_api/platform_detect.py`, `services/cli_artifacts.py`; `fast_api/server.py` (settings, lifespan task, registration, `Accept-CH`) |
| Access control | `access_control/config.py` (`_ALWAYS_UNAUTH_PATHS` + merge + log); `access_control_config.yaml`, `.standalone`, `.disabled` |
| UI | **new** `components/CliDownloadModal.tsx` + test; `components/AppLayout.tsx`, `pages/HomePage.tsx`, `pages/LoginPage.tsx`, `services/api.ts`, `types/` |
| Build & CI | `Dockerfile` (bake artifacts; optional toolchain variant), **new** `.github/workflows/cli-artifacts.yml` |
| Docs | `docs/cli.md`, `site/cli.html`, `docs/config-env-vars.md`, `container.env`, `README.md`, `CHANGELOG.md` |

**Layout note (as built).** §4.1 sketches the branding layer as a single
`package main`. The implementation splits it in two, because Go requires it:

```
client/go/
  cli/            package cli   — the implementation, exported API
  cli/cmd/sbs/    package main  — the entry point, ~20 lines
  tests/          package tests — the test suite
```

A `package main` is *"a program, not an importable package"* — the compiler
refuses the import outright — so a test living in any directory other than the
code's own can only exist if the implementation is a library. Keeping `main` to
an entry point is therefore the precondition for the test suite's location, not a
stylistic choice.

One consequence worth stating: the identifiers the tests exercise are **exported**
(`BrandPaths`, `BaseURL`, `LocalVerb`, `StandaloneAuth`, …), which is a wider API
surface than a single `main` package would have needed. The `-ldflags -X` targets
move with them — from `main.urlSlot` to
`github.com/skillberry-ai/skillberry-store/client/go/cli.URLSlot`. That path is
load-bearing: `-X` with a wrong package path is accepted **silently** by the
linker and injects nothing, so it is a named constant on the Python side
(`GO_LDFLAGS_PKG`) and asserted against the Go source by
`test_ldflags_target_matches_the_go_package`.

| Phase | Content | Why this order |
| --- | --- | --- |
| **1** | `client/go` with branding, promoted surface, auth handler, verbs; CI build + the "no restish" assertion; **delete the Python shim and its generation step** | Delivers Feature A entirely, with no server change and no new surface. The deletion lands here, not later, so there is never a release with two CLIs |
| **2** | `SBS_PUBLIC_URL`, `CliArtifactService` with `patch`, `/cli/manifest`, `/cli/download`, ACL floor, `sbs download-cli` | The whole download loop, no toolchain in the image |
| **3** | Platform wheels + the `[cli]` extra, install scripts, UI modal (masthead, home card, **login screen**), sidecar for `darwin-arm64`, optional `rebuild` mode | Distribution and polish on a working contract |
| ~~**4**~~ | ~~Signing/notarization, provenance, upstream PR for the two flag strings~~ | **Dropped** (review decision). Unsigned binaries are documented rather than worked around; the two residual flag strings stay as they are |

Rollback: `SBS_CLI_DOWNLOAD=off` removes the server-side feature at any time. The CLI itself rolls back the way any release does — by installing the previous version — which is why the deletion in Phase 1 is safe: the last shim release remains installable for anyone who pins it.

### 9.1 Migration and the one breaking change

`pip install skillberry-store-sdk` currently installs an `sbs` console script ([dev.mk:224-230](../../skillberry-common/.mk/dev.mk#L224-L230)). After Phase 1 it does not. That is the only user-visible break, and it is deliberate.

| Audience | Before | After |
| --- | --- | --- |
| SDK users who never ran `sbs` | pure-Python SDK | unchanged |
| `pip` users who want the CLI | `pip install skillberry-store-sdk` | `pip install skillberry-store-cli` (or `skillberry-store-sdk[cli]`) — a native binary, no restish on `PATH` |
| Users on a platform with no wheel | shim + a manual restish install | `curl -fsSL <store>/cli/install.sh \| sh`, or the UI download |
| Anyone pinned to the old SDK | works | works; the shim release is not withdrawn |

Required by this design, not optional:

* A `BREAKING:` entry in [CHANGELOG.md](../../CHANGELOG.md) naming the remedy, with the identifier a deployer would search for (`sbs console script`). [test_changelog.py](../../src/skillberry_store/tests/test_changelog.py) exists precisely to keep such notes findable, so the entry is part of the change, not follow-up paperwork.
* `docs/cli.md`, `site/cli.html` and `README.md` lose every "install restish" instruction in the same PR — a docs-currency test guards this (§8.3 #22), in the style of `test_ui_docs_current.py`.
* One release cycle where both `skillberry-store-cli` wheels and the last shim release are installable, so a user upgrading on a Monday has a one-line fix and not a broken CI job.

---

## 10. Rejected alternatives

Recorded with their measurements so they are not relitigated, and so nobody mistakes either for a fallback.

### 10.1 PyInstaller over the Python shim — do not implement

This was the original plan and the first version of this document. It is recorded **only** so that the measurements behind the decision are not lost, and so nobody re-proposes it. It is not a fallback, not a phase, and not an option if Go turns out to be inconvenient: the Python shim it depends on is deleted by this design (§4.6), and PyInstaller appears nowhere in the implementation.

**Shape.** Keep the Python shim; scrub `restish` out of its output by piping restish's streams (stderr always, stdout only on help paths) through a rewriter with a protected-span list and a reserved-command passthrough so rewritten hints stay executable; freeze the shim with PyInstaller, bundling the restish binary via `--add-binary`.

**Measured** (PyInstaller 6.22.3, CPython 3.11.15, restish 2.3.0, Linux x86-64):

| Finding | Value |
| --- | --- |
| Onefile with restish bundled | 20.3 MB, 7.3 s, 88.9 MB peak RSS |
| Per-invocation startup overhead (unpacking 33 MB to temp) | **0.17 s**, every command |
| A module-level URL constant patchable in the binary? | **No** — the PYZ is zlib-compressed |
| Appended trailer (18 B / 64 KiB / 2 MB) | All still run; a frozen app can read its own trailer |
| `argv[0]` rename to hide "restish" | **No effect** — the name is a literal in restish |

**Why it lost.**

1. **PyInstaller cannot cross-compile.** Five artifacts needed five native CI runners, and the server could only ever build its own platform — so the requested "build at server start for every platform" was not implementable. Go builds all five in one place, 2.3 s each per URL.
2. **The scrubber was a permanent tax** — ~150 lines, a protected-span list, a reserved passthrough, golden fixtures pinned to a restish version, and a documented risk of corrupting user data. The library route deletes all of it and leaves four known strings.
3. **Two binaries in a trench coat.** The artifact carried CPython *and* the restish executable, paid 0.17 s of extraction per command, and inherited the build host's glibc floor. A static Go binary has none of those properties.
4. **URL injection was a hack with a hole.** Appending a trailer worked on Linux and probably Windows, but may break the macOS ad-hoc signature, and the server cannot re-sign from Linux. In the Go design that hole shrinks to one platform in one configuration, and disappears entirely under option **B**.
5. It also *kept* the two-level command shape (`restish sbs list-skills`) that made the branding problem exist in the first place.

Nothing about the *transport* half of that design was wrong, and it is carried over intact: the endpoints, the manifest, platform detection, the ACL floor, the install scripts, the UI modal and the security posture in §5–§7 are the same conclusions reached there.

### 10.2 restish as a C shared/static library called from Python

Asked during review: keep the CLI in Python and call restish through a `.so`/`.dll` (or a linkable `.a`), shipping the packed Python app and the library together. Probed rather than argued — a real `c-shared` build exporting `SbsRun(argc, argv)` was called from Python via `ctypes` and returned live data from a running store, with a **0.03 s** startup (lazy `dlopen`, faster than PyInstaller's extraction). One finding is in the idea's favour and worth keeping on record: **restish never calls `os.Exit` internally** (0 call sites across `exit.go`, `cli.go`, `root.go`, `setup.go`, `dispatch.go`, `completion.go`, `plugin_install.go`); it returns a typed `ExitCodeError`, so it is well-behaved inside a host process.

It still loses, for four measured reasons:

| # | Finding |
| --- | --- |
| 1 | **`c-shared` and `c-archive` require cgo**, which ends single-box cross-compilation. Verified: `CGO_ENABLED=0` refuses (`requires external (cgo) linking`); `GOOS=windows` needs mingw-w64 (`unrecognized option '-mthreads'`), `GOOS=darwin` needs clang + Apple's SDK (`unrecognized option '-arch'`), and even `linux/arm64` needs an aarch64 assembler. Upstream demonstrates the same constraint: every goreleaser build is `CGO_ENABLED=0` **except** `restish-pkcs11`, which is split into five per-platform build ids |
| 2 | **It reinstates PyInstaller** — and with it five native CI runners, 0.17 s extraction per command, the build host's glibc floor, and the compressed-PYZ problem that forced the trailer/sidecar URL hacks (§10.1) |
| 3 | **The C ABI cannot carry the API this design uses.** Only C types cross, so `SetDefaultConfig`, `SetCommandSurface` and `AddAuthHandler` (a Go *interface*) are unreachable from Python; the probe's `lib.go` had to hold all branding and config in Go while Python got ten lines of argv marshalling. A Python auth handler would need Go→Python callbacks across the GIL from a Go-owned thread. The result is two languages plus a hand-written ABI, with the logic still in Go |
| 4 | **Bigger, not smaller**: the `.so` measured **48.4 MB** against a 32 MB static binary (cgo linking cannot drop unreachable code the way the pure-Go link does), before adding a packed interpreter |

A linkable `.a` is the same trade plus wheel-build friction: on Windows, Go's `c-archive` emits mingw-format objects that do not link cleanly against MSVC-built CPython.

**And there is no prebuilt library to download.** Checked exhaustively: release v2.3.0 ships 30 assets, all `.tar.gz`/`.zip` archives of *executables* (`restish` plus four plugin binaries) and `checksums.txt` — zero `.so`/`.dll`/`.dylib`/`.a`, and `-buildmode` appears nowhere in `.goreleaser.yml`, the `Makefile` or `docs/release-packaging.md`. Nor could a useful one exist without upstream work: a Go `c-shared` object exports only the functions *its own* main package marks `//export`, and restish marks none — its public surface is deliberately Go-typed. The library the project does offer is the **Go module**, which is what §3 consumes. Their cross-language extension mechanism is out-of-process plugins (`restish-bulk`, `restish-csv`, `restish-mcp` are separate executables), not FFI.

**Where this would be the right tool:** if the *server* or a plugin ever needs restish's request pipeline callable from Python. That is an additive, independent decision, and it must not reshape the CLI, whose requirement is a single native executable on a machine with no Python.

**On "no Go toolchain":** the goal is already met. The runtime image needs none — CI links the five static binaries once per release and the server bakes each deployment's URL by patching the slot in place (M9). Zero Go *anywhere* is only possible by shipping upstream's stock binary unmodified, which is today's state and is exactly what leaves Feature A unsolved (§2). Branding costs one link per release, in CI; this alternative would raise that bill to Go + cgo + mingw-w64 + aarch64-gcc + clang with Apple's SDK.

---

## 11. Issue index

| Ref | One line | Where |
| --- | --- | --- |
| G1–G10 | Two unhookable upstream strings, a wrong `doctor` hint, offline promoted-root help, depending on a "first pass" API, pip distribution, a shared-subtree template, the breaking change for today's `sbs` users, the `-ldflags` no-op trap, config migration, a new toolchain dependency | §4.7 |
| §10.1–10.2 | Why PyInstaller-over-the-shim and a C-shared library behind a Python CLI were both rejected, with the measurements | §10 |
| B1–B18 | Cross-compilation, URL baking with and without a toolchain, the `darwin-arm64` signature, image size, a compiler in production, cache lifetime, concurrency, the ACL override footgun, platform detection, caching, bandwidth, traversal, `Host`/linker injection, spec drift, stale registration, quarantine/AV, verifiability | §5.11 |

---

## 11.1 Revisions after review

Recorded here, and only here: the rest of the repository describes the final state.
This section exists so that a reader who finds an earlier draft — or wonders why a
section above proposes something the code does not do — can see what changed and
why.

| # | §  | Proposed | Built | Why |
| --- | --- | --- | --- | --- |
| R1 | §5.5.1 | Five endpoints: `manifest`, `download`, `install.sh`, `install.ps1`, `license` | **One**: `GET`/`HEAD` `/cli/download`, variants by query argument | Each extra route was a separate public surface to allow-list, cache, document and keep in step. The manifest existed to carry an artifact's identity, which response headers do more directly; the install scripts were a generated shell program — the sharpest security object in the feature (§5.7) — for something `curl -o` already does; and a licence endpoint is a download API for a text file that belongs in the repository |
| R2 | §5.5.1 | `sha256` read from the manifest before downloading | `X-SBS-SHA256` on the download response | Same request that carries the bytes, so there is no window in which the digest and the artifact could disagree, and no second round trip. `HEAD` still reveals it without transferring ~32 MB. A `format=json` variant was the alternative; a header beats it because it needs no extra request at all |
| R3 | §7.4 | restish's MIT licence served at `/cli/license` | Reproduced in the repository `LICENSE` under *Third-party software notices*, pointed at by `sbs --version`, and shipped in the `archive` variant | Redistribution obligations are met by the notice travelling with the artifact and the source, not by an HTTP endpoint |
| R4 | §5.5.3 | A separate `_ALWAYS_UNAUTH_PATHS` floor merged over the operator's list | `/cli*` is one of the `_DEFAULT_UNAUTH_PATHS`, and a config file's list **adds to** the defaults instead of replacing them | One mechanism instead of two. This fixes the replace-vs-extend footgun at its source, which is what made the second list necessary in the first place. **Consequence:** an operator can no longer drop a default — the list only widens. That is deliberate, and it is what stops a config edit from making a deployment unable to answer its own liveness probe |
| R5 | §6 | Eight `SBS_CLI_*` variables | **One**: `SBS_CLI_DOWNLOAD` | Everything else was derivable. The directories are a function of the source tree; the mechanism is a function of the platform and of whether a toolchain exists; preparation runs when the feature is on. A setting that only ever has one sensible value is a way to misconfigure a deployment, not a way to configure it |
| R6 | §5.2 | `auto` means "rebuild wherever a toolchain exists" | `auto` means "the cheapest mechanism that works, per platform": patch for four platforms, rebuild only for `darwin-arm64` | Patching is milliseconds and produces a binary indistinguishable from a rebuilt one, so cross-compiling those four buys nothing and costs tens of seconds each. Measured: with a toolchain present the first reading made every server start a five-platform cross-compile, minutes of CPU, and made the test suite unusable |
| R7 | §9 | Phase 4: signing, notarization, provenance, upstream PR | **Dropped** | Unsigned binaries are documented (B17) rather than worked around |
| R8 | §5.9 | sha256 shown in the modal beside the download control | Behind a *Verify this download* disclosure | A 64-character hex string next to the primary action is clutter for everyone who will not check it. The disclosure also carries the command whose output it should equal |
| R9 | §5.3, §7.3 | An artifact is published `ready` after its raw binary is written; one per-IP request budget covers the endpoint | `ready` is published only once every variant for that platform exists; the budget is charged per request kind — 30 GETs, 300 HEADs | Both were found by the CI smoke job rather than by unit tests, because both need a real client's timing. `state` is what the route reads, so setting it before the archive was built advertised `format=archive` for a file still being gzipped — around 1.5 s for a 33 MB binary. And a ~650-byte HEAD spending the same allowance as a 33 MB GET protects nothing against amplification while breaking the two access patterns §5.6 and §7.3 encourage, reading the digest before downloading and polling until ready |
| R10 | §9 phase 3 #2 | `darwin-amd64` executes on `macos-13` | `macos-15-intel` | `macos-13` was retired on 2025-12-04; the label takes no runner, so the job queued indefinitely instead of failing. `macos-15-intel` is the documented x86_64 successor and keeps the check on a genuine Intel host rather than moving it to Rosetta. It is itself due to retire in August 2027 |

## 12. Future work / open questions

* **Signing**: Authenticode for Windows, Developer ID + notarization for macOS. Removes the SmartScreen/Gatekeeper friction (B17) and would let `darwin-arm64` use `patch` if re-signing happened at preparation time.
* **Upstream PR** for the two flag descriptions and `doctor`'s label/hint (G1, G2) — small, and aligned with upstream's own accepted requirement that embedded surfaces not say "Restish".
* **`windows-arm64`**: one more `GOARCH`; gated only on wanting to execute it in CI.
* **SLSA provenance / SBOM** for the artifacts (B18); shell completions are already upstream's (`sbs cli completion`), so the gap the previous design had here is closed.
* **Homebrew tap / winget / apt**, once the artifacts are signed. The manifest already carries everything a formula needs (per-platform URL, sha256, version).
* Two questions this design deliberately settled rather than deferred: the login screen **does** offer the download (§5.9), and the Python CLI **is** deleted rather than kept as a fallback (§4.6).
