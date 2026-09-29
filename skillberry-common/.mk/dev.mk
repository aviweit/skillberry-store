##@ Development

# List here all supported Python version specs (one or more separated by space). 
# Spec options: v1 v1.v2 v1.v2.v3
# Can add "+" to specify minimal version
# Example: 3.13+ 3.12.9 3.11.5+
SUPPORTED_PYTHON_VERSIONS := 3.11 3.12+

# Service name in lowercase
SERVICE_NAME_LC = $(shell printf "%s" "$(SERVICE_NAME)" | tr '[:upper:]' '[:lower:]')
# Service name in code notation - lowercase + replace hyphen->underscore
SERVICE_NAME_CN ?= $(shell printf "%s" "$(SERVICE_NAME_LC)" | tr '-' '_')

ACRONYM_LC ?= $(shell echo $(ACRONYM) | tr '[:upper:]' '[:lower:]')

RESTISH_CONFIG_APIS ?= $(HOME)/.config/restish/apis.json

OPEN_API_SPEC_URL ?= http://$(SERVICE_HOST):$(MAIN_SERVICE_PORT)

export SERVICE_HAS_SDK ?= 0

# Change detection is driven off the BUILD_VERSION label (see globals.mk and
# concept 4 of docs/design/build_concepts.md). No parallel file-scan is used.

git-hooks-setup:
	@if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then \
	    echo "Setting up Git hooks..."; \
	    git config core.hooksPath .githooks; \
	    chmod +x .githooks/*; \
	else \
	    echo "Skipping git-hooks-setup: not inside a Git repository."; \
	fi

.PHONY: show-srv-env
show-srv-env: .stamps/srv.env	## Show service env (ports, host)
	@cat .stamps/srv.env

test: ## Run tests
	@$(MAKE) install-requirements ODEPS=test SKIPOPT=1
	pytest

test-e2e: ## Run end-to-end tests
	@$(MAKE) install-requirements ODEPS=test SKIPOPT=1
	pytest src/skillberry_store/tests/e2e

check-git-clean:
	@changes="$$(git status --porcelain)"; \
	if [ -n "$$changes" ]; then \
	  echo "! You have uncommitted changes. Please commit, stash or clean them before releasing."; \
	  echo "=== Changes ==="; \
	  echo "$$changes"; \
	  exit 1; \
	fi

check-git-main:
	@if [ "$(shell git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
		echo "! You must be on the main branch to run this command"; \
		exit 1; \
	fi

.PHONY: install-requirements verify-venv
install-requirements: git-hooks-setup verify-venv .stamps/git-version-manifest .stamps/install-requirements-$(ODEPS) ## Install dependencies. Opt: make install-requirements ODEPS=dev [SKIPOPT=1 to allow skip]
	@true

verify-venv:
	@$(SB_COMMON_PATH)/scripts/check_venv.sh $(SUPPORTED_PYTHON_VERSIONS) || exit 1
	@python $(SB_COMMON_PATH)/scripts/ensure_pip.py || exit 1
	@python -m pip install uv

# Install stamp: prerequisite is exactly `pyproject.toml`. The per-ODEPS stamp
# name captures the ODEPS variant (concept 5). Concept-6 stamp bookkeeping
# (single valid non-empty stamp, always-valid empty stamp, no touch on
# failure) is performed by the delegated script.
.stamps/install-requirements-$(ODEPS): pyproject.toml
	@$(SB_COMMON_PATH)/scripts/install-requirements.sh "$(ODEPS)" "$(SKIPOPT)"


# .stamps/git-version-manifest is the pivot of the build stamp graph — the
# label-derived record that gates all downstream builds (concepts 2 & 4).
#
# The recipe runs whenever a make target that (transitively) depends on the
# manifest is invoked (e.g. install-requirements, docker-build). It calls
# scripts/git_state.py update, which is content-idempotent: the manifest
# file is rewritten (and observability printed to stderr) only when
# repository state has actually changed, keeping mtime stable so downstream
# targets don't refire spuriously. Targets that don't need the label (make
# help, make print_build_version, etc.) don't trigger this recipe.
.stamps/git-version-manifest: FORCE
	@python $(SB_COMMON_PATH)/scripts/git_state.py update \
	    "$(BUILD_VERSION)" "$@" "$(VERSION_LOCATION)"

.PHONY: FORCE
FORCE:


release: check-git-main check-git-clean install-requirements  ## Release a new version (REDO=1 to cleanup existing artifacts)
	@if [ -z "$(RELEASE_VERSION)" ]; then \
		echo "++++++++++++++++++++++++++++++++++++++++++++"; \
  		echo "RELEASE_VERSION is not set. It is required for the release"; \
  		echo "Please set RELEASE_VERSION and use 'RELEASE_VERSION=<version> make release' "; \
		echo "++++++++++++++++++++++++++++++++++++++++++++"; \
	exit 1; fi

	@command -v sed >/dev/null 2>&1 || { echo "❌ 'sed' is not installed. Aborting."; exit 1; }
	@echo "++++++++++++++++++++++++++++++++++++++++++++"
	@echo "=> Creating release with version: $(RELEASE_VERSION)"
	@if [ "$(REDO)" = "1" ]; then \
		echo "=> REDO mode enabled - cleaning up existing artifacts"; \
	fi
	@echo "++++++++++++++++++++++++++++++++++++++++++++"
	@sleep 10

	# REDO: Clean up existing artifacts if REDO=1
	@if [ "$(REDO)" = "1" ]; then \
		echo "===> REDO: Cleaning up existing local branch branch-$(RELEASE_VERSION)"; \
		git branch -D branch-$(RELEASE_VERSION) 2>/dev/null || echo "Local branch does not exist"; \
		echo "===> REDO: Cleaning up existing remote branch branch-$(RELEASE_VERSION)"; \
		git push origin --delete branch-$(RELEASE_VERSION) 2>/dev/null || echo "Remote branch does not exist"; \
		echo "===> REDO: Cleaning up existing local tag $(RELEASE_VERSION)"; \
		git tag -d $(RELEASE_VERSION) 2>/dev/null || echo "Local tag does not exist"; \
		echo "===> REDO: Cleaning up existing remote tag $(RELEASE_VERSION)"; \
		git push origin --delete $(RELEASE_VERSION) 2>/dev/null || echo "Remote tag does not exist"; \
		echo "===> REDO: Cleaning up existing GitHub release $(RELEASE_VERSION)"; \
		gh release delete $(RELEASE_VERSION) --yes 2>/dev/null || echo "GitHub release does not exist"; \
		echo "===> REDO: Cleaning up existing Docker images"; \
		$(DOCKER) rmi -f $(FULL_IMAGE_NAME):$(RELEASE_VERSION) 2>/dev/null || echo "Docker image with version tag does not exist"; \
		echo "===> REDO: Cleaning up docker build/get stamps so the re-release forces a fresh build"; \
		rm -f .stamps/docker-build-registry-* .stamps/docker-get-* 2>/dev/null || true; \
		echo "===> REDO: Cleanup completed"; \
	fi

	@echo "===> Generating git tag $(RELEASE_VERSION) and creating GitHub release"
	@git checkout -b branch-$(RELEASE_VERSION)
	@echo "===> Generated release branch $(RELEASE_VERSION)"
	@git tag -a $(RELEASE_VERSION) -m "Release $(RELEASE_VERSION)"
	# Push BOTH the tag and the release branch. The tag is what git_state.py's
	# release detection resolves the commit count against, and the branch is the
	# release convention (a branch per release, carrying a toml that pins the
	# matching sdk) — pushing only the tag left origin without the branch, which
	# is how the repository ended up with no resolvable release branch at all.
	# Either one lets the docker-build sub-make below recompute BUILD_VERSION as
	# $(RELEASE_VERSION); without them the image is tagged off the *previous*
	# release instead.
	@git push origin $(RELEASE_VERSION) branch-$(RELEASE_VERSION)

	#
	# Switch to main so the subsequent gh release notes command computes its
	# commit range against main rather than the release branch.
	#
	@git checkout main

	#
	# The following block calls either to "basic" gh release command or an "explicit" one:
	# 
	# If no previous release exists then "basic" is called
	# If a previous release exists then "explicit" using commit range is called
	#

	@REL_PREV_RELEASE=$$(git branch -r | grep 'branch-' | sed 's|.*/branch-||' | sort -V | tail -n 2 | head -n 1); \
	if [ -z "$$REL_PREV_RELEASE" ] || [ "$$REL_PREV_RELEASE" = "$(RELEASE_VERSION)" ]; then \
		echo "No previous release found. Creating release with generated notes..."; \
		gh release create $(RELEASE_VERSION) --generate-notes; \
	else \
		echo "Previous release found: $$REL_PREV_RELEASE"; \
		REL_CURRENT_COMMIT=$$(git rev-parse --short=7 HEAD); \
		REL_PREV_COMMIT=$$(git merge-base origin/main origin/branch-$$REL_PREV_RELEASE); \
		echo "Creating release from $$REL_PREV_COMMIT to $$REL_CURRENT_COMMIT..."; \
		gh release create $(RELEASE_VERSION) --title "$(RELEASE_VERSION)" --notes "$$(git log --pretty=format:'- %s by %an' $$REL_PREV_COMMIT..$$REL_CURRENT_COMMIT)"; \
	fi

	#
	# Important: change back to release branch so that docker image is built with customized
	# toml/requirement files
	#
	@git checkout branch-$(RELEASE_VERSION)

	@echo "===> Building and pushing new docker image"
	@DBT=registry make docker-build
	@echo "++++++++++++++++++++++++++++++++++++++++++++"
	@echo "=> Release $(RELEASE_VERSION) created successfully"
	@echo "++++++++++++++++++++++++++++++++++++++++++++"


update-sdk: ## Update the SDK, if needed
	@if [ "$$SERVICE_HAS_SDK" = "1" ]; then \
		rm -rf /tmp/skillberry-sdk || true; \
		echo "==> Updating SDK..."; \
		make docker-run; \
		timeout 120 bash -c 'until curl -sf $(OPEN_API_SPEC_URL)/docs > /dev/null; do echo "Waiting for $(DESC_NAME)..."; sleep 5; done'; \
		echo "$(DESC_NAME) started (using docker)"; \
		make generate-sdk && \
		echo "SDK updated successfully" && \
		git add . && \
		if git diff --cached --quiet; then \
			echo "!!! No updates to commit !!!"; \
		else \
			echo "!!! Updates detected, committing... !!!"; \
			git commit -m "Update $(SERVICE_NAME_CN)_sdk $$(date '+%Y-%m-%d %H:%M:%S')"; \
		fi; \
		make docker-stop; \
		echo "$(DESC_NAME) stopped"; \
		echo "==> SDK update completed successfully"; \
	else \
		echo "Service has no SDK, skipping"; \
	fi

PYTHON_SDK_DIR = client/python/$(SERVICE_NAME_CN)_sdk

# Whether to inject the Python `restish` shim CLI (scripts/sdk_cli.py) into the
# generated SDK and declare it as a console script.
#
# Defaults to 1 so every asset that relies on the shim today keeps its CLI with
# no change. An asset that has replaced the shim with its own CLI sets
# `SDK_PY_CLI := 0` in .mk/local.mk; skillberry-store does, because its `sbs` is
# now a native Go binary that embeds restish as a library rather than shelling
# out to it (skillberry-store docs/design/new_cli.md §4.6 / G6).
#
# The template itself is deprecated: the subprocess model it is built on is what
# made the CLI's output impossible to brand. It is removed from
# skillberry-common once no asset sets SDK_PY_CLI=1.
SDK_PY_CLI ?= 1

generate-sdk: install-requirements # Generate SDK
	@mkdir -p $(PYTHON_SDK_DIR)
	@rm -fr $(PYTHON_SDK_DIR)/*
	@openapi-generator-cli generate -i $(OPEN_API_SPEC_URL)/openapi.json \
		-g python \
		-o $(PYTHON_SDK_DIR) \
		--package-name $(SERVICE_NAME_CN)_sdk
	@echo "==> Backing up setup.py and pyproject.toml"; \
		cp $(PYTHON_SDK_DIR)/setup.py $(PYTHON_SDK_DIR)/setup.py.bak; \
		cp $(PYTHON_SDK_DIR)/pyproject.toml $(PYTHON_SDK_DIR)/pyproject.toml.bak;
	@echo "==> Fixing pyproject.toml build backend to use Poetry..."
	@toml set --to-array --toml-path $(PYTHON_SDK_DIR)/pyproject.toml "build-system.requires" "[\"poetry-core>=1.0.0\"]"
	@toml set --toml-path $(PYTHON_SDK_DIR)/pyproject.toml "build-system.build-backend" "poetry.core.masonry.api"
ifeq ($(SDK_PY_CLI),1)
	@echo "==> Adding CLI module to SDK..."
	@sed -e 's|{{API_NAME}}|$(ACRONYM_LC)|g' \
	     -e 's|{{API_URL}}|$(OPEN_API_SPEC_URL)|g' \
	     $(SB_COMMON_PATH)/scripts/sdk_cli.py > $(PYTHON_SDK_DIR)/$(SERVICE_NAME_CN)_sdk/sdk_cli.py
	@echo "==> Updating setup.py to add CLI entry point..."
	@sed -i '/package_data=/i\    entry_points={\n        "console_scripts": [\n            "$(ACRONYM_LC)=$(SERVICE_NAME_CN)_sdk.sdk_cli:cli",\n        ],\n    },' $(PYTHON_SDK_DIR)/setup.py
	@echo "==> Adding CLI entry point to [tool.poetry.scripts]..."
	@toml add_section --toml-path $(PYTHON_SDK_DIR)/pyproject.toml "tool.poetry.scripts"
	@toml set --toml-path $(PYTHON_SDK_DIR)/pyproject.toml "tool.poetry.scripts.$(ACRONYM_LC)" "$(SERVICE_NAME_CN)_sdk.sdk_cli:cli"
else
	@echo "==> SDK_PY_CLI=0: leaving the SDK a pure Python library (no $(ACRONYM_LC) console script)"
	@echo "==> Adding the [cli] extra pointing at the native CLI wheel..."
	@# The SDK stays pure Python and universal, but `pip install
	@# <sdk>[cli]` should still get the user a working CLI. Environment
	@# markers keep it to the platforms with a published wheel: on anything
	@# else the extra resolves to nothing rather than failing the install.
	@#
	@# Re-applied here on every generation because `generate-sdk` wipes the
	@# SDK directory first -- without this the extra would silently vanish
	@# the next time the SDK is regenerated.
	@python3 -c "$$SDK_CLI_EXTRA_PY" \
		"$(PYTHON_SDK_DIR)" "$(ASSET_NAME)-cli"
endif
	@echo "==> Removing [project.scripts] section if it exists..."
	@sed -i '/^\[project\.scripts\]/,/^$$/d' $(PYTHON_SDK_DIR)/pyproject.toml
	@echo "==> SDK generation complete$(if $(filter 1,$(SDK_PY_CLI)), with CLI support,)"

# Injects the `[cli]` extra into both setup.py and pyproject.toml. A Python
# helper rather than a chain of `sed`/`toml` calls because the value is a
# PEP 508 requirement containing commas, quotes and parentheses -- exactly the
# characters that make shell-quoting through make fragile.
define SDK_CLI_EXTRA_PY
import pathlib, re, sys
sdk_dir, cli_pkg = pathlib.Path(sys.argv[1]), sys.argv[2]
markers = " or ".join([
    "(sys_platform == 'linux' and platform_machine in 'x86_64 aarch64')",
    "(sys_platform == 'darwin' and platform_machine in 'x86_64 arm64')",
    "(sys_platform == 'win32' and platform_machine in 'AMD64 x86_64')",
])
req = f"{cli_pkg}; {markers}"

setup_py = sdk_dir / "setup.py"
if setup_py.is_file():
    text = setup_py.read_text()
    if "extras_require" not in text:
        text = text.replace(
            "    install_requires=REQUIRES,",
            '    install_requires=REQUIRES,\n    extras_require={"cli": ["' + req + '"]},',
            1,
        )
        setup_py.write_text(text)

pyproject = sdk_dir / "pyproject.toml"
if pyproject.is_file():
    text = pyproject.read_text()
    if "[project.optional-dependencies]" not in text:
        block = '[project.optional-dependencies]\ncli = [\n  "' + req + '",\n]\n\n'
        if "[project.urls]" in text:
            text = text.replace("[project.urls]", block + "[project.urls]", 1)
        else:
            text += "\n" + block
        pyproject.write_text(text)
print("    [cli] extra -> " + cli_pkg)
endef
export SDK_CLI_EXTRA_PY

