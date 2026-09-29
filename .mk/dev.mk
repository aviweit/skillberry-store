##@ Development

lint: ## Lint the codebase
	@$(MAKE) install-requirements ODEPS=dev
	black --check --diff --color src/skillberry_store/modules src/skillberry_store/tools src/skillberry_store/fast_api src/skillberry_store/utils || \
		(echo "Lint Failed. Please run 'black src/skillberry_store/modules src/skillberry_store/tools src/skillberry_store/fast_api src/skillberry_store/utils' to fix the issues" && exit 1)

# `generate-sdk` (skillberry-common/.mk/dev.mk) depends on `install-requirements`
# with an unset ODEPS — core dependencies only — but openapi-generator-cli,
# openapi-python-client and toml-cli now live in the [build] extra, and nothing
# earlier installs it. `make update-sdk`, run by ci-push, therefore dies with
# "openapi-generator-cli: command not found".
#
# Add the missing install as an extra prerequisite rather than overriding the
# upstream recipe. It has to be a recursive $(MAKE): the install stamp name
# embeds $(ODEPS) at parse time (.stamps/install-requirements-$(ODEPS)), so
# ODEPS must be set for a fresh parse, not just for a recipe line.
.PHONY: install-build-requirements
install-build-requirements: ## Install the [build] extra (SDK codegen + Rust build tools)
	@$(MAKE) install-requirements ODEPS=build

generate-sdk: install-build-requirements

##@ Native CLI (Go)

# The `sbs` CLI is a Go program that embeds restish as a library
# (docs/design/new_cli.md). It replaces the deleted Python shim, which is why
# `SDK_PY_CLI := 0` is set below: there is exactly one implementation of `sbs`.
# The Go module root. Inside it:
#   cli/          the importable implementation (package cli)
#   cli/cmd/sbs/  the binary's entry point (package main)
#   tests/        the test suite (package tests)
#
# The split is required, not stylistic: Go refuses to import a `package main`
# ("a program, not an importable package"), so tests in their own directory can
# only exist if the implementation is a library. See client/go/cli/version.go.
CLI_GO_ROOT    := client/go
CLI_DIR        := $(CLI_GO_ROOT)/cli
CLI_CMD        := ./cli/cmd/sbs
CLI_PKG        := github.com/skillberry-ai/skillberry-store/client/go/cli
CLI_PREBUILT   := cli-prebuilt
# The closed platform enum of §5.1. The server validates `?platform=` against
# the same list, so adding one here is necessary but not sufficient.
CLI_PLATFORMS  ?= linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64
CLI_SOURCES    := $(wildcard $(CLI_DIR)/*.go) $(wildcard $(CLI_DIR)/cmd/sbs/*.go) \
                  $(CLI_GO_ROOT)/go.mod $(CLI_GO_ROOT)/go.sum

# Locating the Go toolchain.
#
# Nothing here assumes a particular install layout -- Go arrives differently on a
# developer laptop, a CI runner and a container image. Resolution order:
#
#   1. GO=/path/to/go on the command line or in the environment  (explicit wins)
#   2. `go` on PATH                                             (the usual case)
#   3. $GOROOT/bin/go                                           (GOROOT set, bin not on PATH)
#   4. a few conventional install locations                      (last resort)
#
# Set GO explicitly for anything unusual:
#
#   make cli-build GO=/opt/go1.27/bin/go
#
# Resolved to an absolute path because every recipe below `cd`s into the module
# root first, and a relative command would not survive that.
GO ?= go
_GO_BIN := $(shell     if command -v "$(GO)" >/dev/null 2>&1; then command -v "$(GO)";     elif [ -n "$$GOROOT" ] && [ -x "$$GOROOT/bin/go" ]; then echo "$$GOROOT/bin/go";     else         for _c in /usr/local/go/bin/go /opt/go/bin/go /opt/homebrew/bin/go                   "$$HOME/.local/go/bin/go" "$$HOME/go/bin/go"; do             if [ -x "$$_c" ]; then echo "$$_c"; break; fi;         done;     fi)

# Every Go target is guarded on a toolchain being found rather than declaring one
# as a prerequisite. A `pip`-only contributor must still be able to run
# `make test` and `make lint` (§G10): the CLI is a release-time artifact, not a
# prerequisite for working on the store.
_GOFMT_BIN := $(shell \
    if [ -n "$(_GO_BIN)" ] && [ -x "$$(dirname $(_GO_BIN))/gofmt" ]; then echo "$$(dirname $(_GO_BIN))/gofmt"; \
    elif command -v gofmt >/dev/null 2>&1; then command -v gofmt; \
    elif [ -n "$$GOROOT" ] && [ -x "$$GOROOT/bin/gofmt" ]; then echo "$$GOROOT/bin/gofmt"; \
    elif [ -n "$(_GO_BIN)" ]; then echo "$$($(_GO_BIN) env GOROOT)/bin/gofmt"; \
    fi)

_HAVE_GO := $(if $(_GO_BIN),1,)

_NO_GO_MSG := no Go toolchain found. Put go on PATH, set GOROOT, or pass GO=/path/to/go.

.PHONY: cli-toolchain
cli-toolchain: ## Report which Go toolchain the CLI targets would use
ifeq ($(_HAVE_GO),1)
	@echo "go:    $(_GO_BIN)"
	@echo "gofmt: $(_GOFMT_BIN)"
	@$(_GO_BIN) version
else
	@echo "$(_NO_GO_MSG)"
	@exit 1
endif

.PHONY: cli-build cli-test cli-fmt cli-fmt-check cli-vet cli-dist cli-vendor cli-clean

cli-build: ## Build the native sbs CLI for this platform into client/go/sbs
ifeq ($(_HAVE_GO),1)
	@echo "===> Building the native sbs CLI for this platform"
	@# The -X target is the package's full import path, not `main`: the injected
	@# variables live in the importable `cli` package. A stale `-X main.version=`
	@# is accepted silently by the linker and injects nothing.
	@cd $(CLI_GO_ROOT) && CGO_ENABLED=0 $(_GO_BIN) build -trimpath \
		-ldflags "-s -w -X $(CLI_PKG).Version=$(VERSION) -X $(CLI_PKG).EngineVersion=$$($(_GO_BIN) list -m -f '{{.Version}}' github.com/rest-sh/restish/v2 | sed 's/^v//')" \
		-o sbs $(CLI_CMD)
	@echo "===> Built $(CLI_GO_ROOT)/sbs"
else
	@echo "NOTE: $(_NO_GO_MSG) Skipping the native CLI build."
endif

cli-dist: ## Cross-compile the CLI for every platform into cli-prebuilt/ (§5.10)
	@GO="$(_GO_BIN)" ./$(CLI_GO_ROOT)/build.sh --out $(CLI_PREBUILT) --platforms "$(CLI_PLATFORMS)" --version "$(VERSION)"

# Vendoring is NOT committed: measured at 44 MB / 2248 files for restish's
# dependency graph, which is a poor trade in a Python repo when go.mod + go.sum
# already pin every module by hash (that, not the vendor tree, is what makes a
# build reproducible — §7.4). This target materialises it on demand for the
# opt-in air-gapped image variant of §5.4 option B, which wants GOFLAGS=-mod=vendor
# and GOPROXY=off at *runtime* and can vendor at build time.
cli-vendor: ## Materialise client/go/vendor for an offline/air-gapped build (not committed)
	@cd $(CLI_GO_ROOT) && $(_GO_BIN) mod vendor && du -sh vendor

cli-fmt: ## Format the Go sources
ifeq ($(_HAVE_GO),1)
	@cd $(CLI_GO_ROOT) && $(_GOFMT_BIN) -w .
endif

cli-fmt-check: ## Fail if any Go source is unformatted
ifeq ($(_HAVE_GO),1)
	@cd $(CLI_GO_ROOT) && out=$$($(_GOFMT_BIN) -l .); \
		if [ -n "$$out" ]; then \
			echo "Lint Failed. Unformatted Go files:"; echo "$$out"; \
			echo "Please run 'make cli-fmt' to fix the issues"; exit 1; \
		fi
endif

cli-vet: ## Run go vet on the CLI
ifeq ($(_HAVE_GO),1)
	@cd $(CLI_GO_ROOT) && $(_GO_BIN) vet ./...
endif

cli-test: ## Run the Go unit tests for the CLI (§8.1)
ifeq ($(_HAVE_GO),1)
	@echo "===> Running the native CLI Go tests"
	@cd $(CLI_GO_ROOT) && $(_GO_BIN) test ./...
else
	@echo "NOTE: $(_NO_GO_MSG) Skipping the native CLI tests."
	@echo "      Go >= 1.25 is needed to run them; see docs/cli.md."
endif

cli-clean: ## Remove built CLI artifacts
	rm -rf $(CLI_PREBUILT) $(CLI_GO_ROOT)/sbs $(CLI_GO_ROOT)/sbs.exe $(CLI_GO_ROOT)/vendor

.PHONY: cli-wheels
# One wheel per platform, each carrying that platform's binary and tagged so pip
# resolves the right one. Depends on cli-dist because there is nothing to compile
# per platform — the binaries already exist, cross-compiled from one host, which
# is why this is a script rather than a cibuildwheel matrix (§4.6, G5).
cli-wheels: ## Build platform wheels for skillberry-store-cli (needs cli-dist first)
	@test -f $(CLI_PREBUILT)/prebuilt-manifest.json || { \
		echo "No artifacts in $(CLI_PREBUILT). Run 'make cli-dist' first."; exit 1; }
	@$(MAKE) install-requirements ODEPS=build
	python packaging/skillberry-store-cli/build_wheels.py \
		--artifacts $(CLI_PREBUILT) --out dist

# Hook the Go tests into `make test` and the format check into `make lint`, so
# the CLI is covered by the gates the repo already runs rather than by a
# separate command nobody remembers. Both no-op without a Go toolchain.
test: cli-test
lint: cli-fmt-check cli-vet

##@ UI

UI_DIR      := src/skillberry_store/ui
UI_DIST     := $(UI_DIR)/dist
UI_STAMP    := .stamps/ui-build
UI_NM_STAMP := .stamps/ui-node-modules
# Inputs that invalidate the built bundle.
UI_SOURCES  := $(shell find $(UI_DIR)/src -type f 2>/dev/null) \
               $(UI_DIR)/index.html \
               $(UI_DIR)/vite.config.ts \
               $(UI_DIR)/tsconfig.json \
               $(UI_DIR)/tsconfig.node.json
# ACL mode is baked into the bundle at build time by vite.config.ts, so the
# access-control config is a build input too — mirror its path resolution
# here. Wildcard-guarded: an absent config is a supported setup (both
# vite.config.ts and load_config() fall back to mode=disabled), so it must
# not become a hard "No rule to make target" failure.
ACL_CONFIG  := $(wildcard $(or $(SBS_ACCESS_CONTROL_CONFIG),access_control_config.yaml))

.PHONY: ui-build ui-clean ui-dev ui-typecheck ui-test
ui-build: $(UI_STAMP) ## Build the UI static bundle (idempotent, stamp-based)

# Bundle only — no `tsc` prefix. Vite/esbuild strips types without checking,
# which matches how `npx vite` (dev) and `vitest` have always run. Type
# checking is a separate concern; use `make ui-typecheck` as a CI gate.
$(UI_STAMP): $(UI_SOURCES) $(UI_NM_STAMP) $(ACL_CONFIG)
	@echo "===> Building UI static bundle"
	@cd $(UI_DIR) && npx vite build
	@mkdir -p .stamps && touch $@

ui-typecheck: $(UI_NM_STAMP) ## Run TypeScript type checking on the UI (not part of ui-build)
	@cd $(UI_DIR) && npm run typecheck

# The vitest suite is not part of `make test` (pytest-only, and it must run
# without a node toolchain) and no CI workflow runs it, which is how several
# assertions rotted unnoticed. NOTE: the suite currently has pre-existing
# failures unrelated to the /api normalisation work (VMCPServerDetailPage*,
# SkillsPage.cascade-delete, AnthropicSkillImporter, openApiGenerator.error), so
# this is a developer tool, not yet a green gate. The invariant that matters at
# runtime — no dead "/api" URL prefix ever reaching fetch() — is guarded
# statically by src/skillberry_store/tests/test_ui_api_prefix.py, which DOES run
# in `make test`. Pass UI_TEST_ARGS to scope the run:
#   make ui-test UI_TEST_ARGS=src/utils/endpoints.test.ts
UI_TEST_ARGS ?=
ui-test: $(UI_NM_STAMP) ## Run the UI unit tests (vitest; UI_TEST_ARGS to scope, see note)
	@cd $(UI_DIR) && npx vitest run $(UI_TEST_ARGS)

# `dist` is gitignored and neither `make test` nor `make test-e2e` built it, so
# `ui_dist.exists()` was always false in CI: the /ui mount, root redirect, SPA
# fallback, cache headers, traversal guard and the RBAC allow-list entries were
# all dead code during tests, while dev machines that *had* built the bundle
# registered extra routes and audited RBAC differently (issue #7). Build it
# before the test targets so both see the same route table.
#
# Best-effort by design: a checkout with no node toolchain must still be able to
# run the Python suite, and the tests that need the real bundle skip themselves
# when it is absent.
.PHONY: ui-build-optional
ui-build-optional: ## Build the UI bundle if a node toolchain is present (used by the test targets)
	@if command -v npm >/dev/null 2>&1; then \
		$(MAKE) ui-build; \
	else \
		echo "NOTE: npm not found - skipping the UI bundle build; /ui tests will skip."; \
	fi

$(UI_NM_STAMP): $(UI_DIR)/package.json $(UI_DIR)/package-lock.json
	@echo "===> Installing UI dependencies"
	@cd $(UI_DIR) && (test -d node_modules || npm ci)
	@mkdir -p .stamps && touch $@

ui-clean: ## Remove UI build artifacts and node_modules
	rm -rf $(UI_DIST) $(UI_STAMP) $(UI_NM_STAMP) $(UI_DIR)/node_modules

ui-dev: $(UI_NM_STAMP) ## Run the Vite dev server with HMR (uses file watchers)
	@cd $(UI_DIR) && npx vite --host 0.0.0.0 --port $${VITE_UI_PORT:-8002}


##@ Container environment

# Application env vars baked into the image, from $(CONTAINER_ENV_FILE).
#
# The service picks them up because tools/configure.py calls python-dotenv's
# load_dotenv() at import time, and its search walks up from the package
# directory to /app/.env. That places the values in os.environ before any
# setting is read, so they are set under a plain `docker run <image>`, under a
# k8s/OpenShift `command:` override, and without going through `make run` --
# none of which is true of a value exported by a makefile.
#
# load_dotenv() does not override an already-set variable, so `docker run -e`,
# `--env-file` and a k8s `env:` entry still win, exactly as they do over a
# Dockerfile ENV. The file is a source of deployment *defaults*, not an
# override, and it must hold no secrets: it is baked into the image.
#
# The image cannot just carry the repo's own .env -- that name is in
# .dockerignore (and .gitignore), so it never enters the build context. Hence a
# separate, version-controlled file copied to /app/.env via the EXTRA_COPY_FILES
# hook, which also grants gid 0 the owner's access -- what makes the file
# readable under the arbitrary UID OpenShift assigns.
CONTAINER_ENV_FILE ?= container.env

# A literal comma cannot appear unescaped in a $(if ...) argument.
_cef_comma := ,

# Guarded on the file existing: stage-extra-copy.sh fails hard on a missing
# source, so an absent (or renamed) env file must drop out of the spec rather
# than break every image build.
#
# `override`, and the existing value folded in, because EXTRA_COPY_FILES is
# itself a documented user-facing knob: a plain assignment here would be
# silently discarded by `make docker-build EXTRA_COPY_FILES=<pair>` -- and the
# env file would then vanish from the image just because the caller also asked
# to copy something else.
ifneq ($(wildcard $(CONTAINER_ENV_FILE)),)
override EXTRA_COPY_FILES := $(EXTRA_COPY_FILES)$(if $(strip $(EXTRA_COPY_FILES)),$(_cef_comma))./$(CONTAINER_ENV_FILE):/app/.env
endif


##@ Docker image variants

# The default image is core-only (Dockerfile sets ARG PLUGIN_EXTRAS= empty).
# This builds the companion variant carrying every bundled plugin, tagged
# :<version>-full / :latest-full so both can coexist in the registry. Run it
# with `make docker-run IMAGE_TAG_SUFFIX=-full`.
.PHONY: docker-build-full
docker-build-full: ## Build the all-plugins image variant (tagged -full)
	@$(MAKE) docker-build \
		IMAGE_TAG_SUFFIX=-full \
		EXTRA_BUILD_ARGS='--build-arg PLUGIN_EXTRAS=plugins-all'

# `ci-push` in skillberry-common builds and pushes only the default image, which
# is now core-only — so :$(VERSION)-full / :latest-full, the tags the #308
# BREAKING note tells deployments to switch to, would never exist in the
# registry. Hook the -full variant into ci-push as an extra prerequisite (same
# pattern as `run: ui-build` in .mk/process.mk, since the ci-push recipe itself
# lives in skillberry-common).
#
# It depends on ci-pull-request so lint/test/test-e2e always pass *before* an
# image is pushed, regardless of prerequisite ordering: .mk/dev.mk is read
# before skillberry-common/.mk/ci.mk, so this prerequisite is evaluated ahead of
# ci-push's own. Make runs each target at most once per invocation, so
# ci-pull-request is not repeated.
.PHONY: ci-docker-build-full
ci-docker-build-full: ci-pull-request ## Build & push the all-plugins (-full) image; run automatically by ci-push
	@echo "|||====> Executing make docker-build-full (buildx multi-platform - also push)"
	VERSION=$(VERSION) DBT=registry $(MAKE) docker-build-full
	@echo "|||====> docker-build-full Done."
	@echo ""

ci-push: ci-docker-build-full
