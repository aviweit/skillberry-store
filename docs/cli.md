# Skillberry Store CLI Documentation

`sbs` is the Skillberry Store command-line interface: a **single native executable** with no runtime dependencies. Its commands are generated from the store's own OpenAPI spec, so they always match the store you are pointed at.

## Overview

- **Auto-generated commands** for all API endpoints
- **Type-safe parameters** based on the OpenAPI schema
- **Automatic documentation** from API descriptions
- **Multiple output formats** (JSON, YAML, table)
- **No prerequisites** — one static binary; no Python, no `pip`, no separate REST client to install
- **Pre-configured** — a binary downloaded from a store already talks to that store

Internally `sbs` embeds [restish](https://rest.sh/) as a Go library. That is an implementation detail: you never install, configure or invoke restish yourself. Its MIT licence is reproduced in this repository's [LICENSE](../LICENSE) under *Third-party software notices*, and `sbs --version` points at it.

> **Upgrading from an older release?** `pip install skillberry-store-sdk` used to install an `sbs` script that required a manual `restish` install. It no longer provides `sbs` at all — see [Installation](#installation) for the replacements. The generated SDK remains a pure Python library.

## Installation

### From a running store (recommended)

Any store serves the CLI, unauthenticated, for every supported platform. One
endpoint, one query argument:

```bash
curl -fsSL "http://localhost:8000/cli/download?platform=linux-amd64" -o sbs
chmod +x sbs
```

Platform ids are `<goos>-<goarch>`: `linux-amd64`, `linux-arm64`, `darwin-amd64`,
`darwin-arm64`, `windows-amd64`. Omit `platform` and the store detects it from the
request, which is convenient in a browser and a guess everywhere else — a script
should always name it.

The response carries the artifact's sha256 in `X-SBS-SHA256`, so you can check
what you received:

```bash
curl -fsSI "http://localhost:8000/cli/download?platform=linux-amd64" | grep -i x-sbs-sha256
shasum -a 256 sbs
```

`HEAD` (`-I`) answers with the digest, the size and the version **without**
transferring the ~32 MB body, so it is cheap to ask first.

`&format=archive` returns a `.tar.gz` (`.zip` on Windows) instead of a bare
executable. Prefer it for a *browser* download: an archive preserves the
executable bit, and on macOS it avoids the Gatekeeper quarantine attribute that a
directly-downloaded binary picks up.

You can also download from the store's web UI — there is a **Download CLI** button
in the masthead, a card on the home page, and the same icon-only button on the
sign-in screen.

### With pip

```bash
pip install skillberry-store-cli
```

A platform wheel carrying the same native binary (the pattern `ruff` and `uv`
use). A wheel has no store baked in, so point it at one once:

```bash
sbs connect https://store.example.com
```

To get the SDK and the CLI together:

```bash
pip install 'skillberry-store-sdk[cli]'
```

### With an existing `sbs`

```bash
sbs download-cli --platform darwin-arm64   # fetch a build for another machine
sbs self-update                            # replace this binary with the store's
```

Both read the expected sha256 from the store's response and verify the bytes
before writing anything.

### Supported platforms

`linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`, `windows-amd64`. A
`HEAD` on the download endpoint tells you whether a given store has a particular
build ready: `200` yes, `404` no build, `503` still being prepared.

### Unsigned binaries

The artifacts are not code-signed or notarized, so:

- **macOS** quarantines a *browser* download. Either fetch it with `curl` (which
  sets no quarantine attribute), or clear it:
  `xattr -dr com.apple.quarantine ~/Downloads/sbs`
- **Windows** SmartScreen may warn on first run.

## Basic Usage

```bash
sbs --help                    # Show all available commands
sbs <command> [args] [flags]  # General command structure
```

## Configuration

### Default Connection

A binary downloaded from a store is already configured to talk to that store — no `connect` step. A binary built from source defaults to `http://localhost:8000`.

Configuration lives in `~/.config/sbs/sbs.json` (created on first use, mode `0600` because it can hold a token), with the spec cache under `~/.cache/sbs`. Run `sbs cli doctor` to see every resolved path.

If you used the previous `sbs`, its `apis.sbs` entry is copied out of `~/.config/restish/restish.json` once, automatically, with a note on stderr. The old file is left untouched.

### Connecting to Different Servers

To connect to a different server:

```bash
sbs connect http://production-server:8000
sbs connect https://staging.example.com
```

The CLI will remember this connection for future commands.

## Command Reference

### Skills Management

```bash
# List all skills
sbs list-skills

# Get a specific skill
sbs get-skill <skill-name>

# Create a new skill
sbs create-skill --name my-skill --description "My skill"

# Update a skill
sbs update-skill <skill-name> --description "Updated description"

# Delete a skill
sbs delete-skill <skill-name>

# Search skills semantically
sbs search-skills --search-term "data processing"

# Anthropic skill import/export
sbs detect-anthropic-skills
sbs import-anthropic-skill
sbs export-anthropic-skill <skill-name>
```

### Tools Management

```bash
# List all tools
sbs list-tools

# Get a specific tool
sbs get-tool <tool-name>

# Get the source module of a tool
sbs get-tool-module <tool-name>

# Search tools semantically
sbs search-tools --search-term "calculator functions"

# Execute a tool
sbs execute-tool <tool-name> --body='{"params":{"x":5,"y":3}}'

# Add a tool from a Python file
sbs add-tool

# Update or delete a tool
sbs update-tool <tool-name>
sbs delete-tool <tool-name>
```

### Snippets Management

```bash
# List all snippets
sbs list-snippets

# Get a specific snippet
sbs get-snippet <snippet-name>

# Create a snippet
sbs create-snippet --name helper --description "Helper utilities"

# Update or delete a snippet
sbs update-snippet <snippet-name>
sbs delete-snippet <snippet-name>

# Search snippets semantically
sbs search-snippets --search-term "string utilities"
```

### VMCP Servers

```bash
# List virtual MCP servers
sbs list-vmcp-servers

# Get a specific VMCP server
sbs get-vmcp-server <server-name>

# Create a VMCP server
sbs create-vmcp-server --name my-server --skill-uuid <uuid>

# Start, update, or delete a VMCP server
sbs start-vmcp-server <server-name>
sbs update-vmcp-server <server-name>
sbs delete-vmcp-server <server-name>

# Search VMCP servers
sbs search-vmcp-servers --search-term "math tools"
```

### vNFS Servers

```bash
# List virtual NFS servers
sbs list-vnfs-servers

# Get a specific vNFS server
sbs get-vnfs-server <server-name>

# Create a vNFS server
sbs create-vnfs-server --name my-server --skill-uuid <uuid>

# Start, update, or delete a vNFS server
sbs start-vnfs-server <server-name>
sbs update-vnfs-server <server-name>
sbs delete-vnfs-server <server-name>

# Search vNFS servers
sbs search-vnfs-servers --search-term "data files"
```

### Admin

```bash
# Liveness + boot stage (always succeeds while the process is up)
sbs health

# Strict readiness (fails until the store is operational)
sbs health-ready

# Prometheus metrics
sbs metrics

# Delete all data (irreversible)
sbs purge-all
```

## Advanced Features

### Output Formats

`sbs` supports multiple output formats:

```bash
# JSON output (default)
sbs list-tools

# YAML output
sbs list-tools -o yaml

# Table output
sbs list-tools -o table

# Raw output
sbs list-tools -o raw
```

### Filtering and Pagination

```bash
# Filter results
sbs list-tools --filter='state=active'

# Limit results
sbs list-tools --limit=10

# Pagination
sbs list-tools --offset=20 --limit=10
```

### Request Body from File

For complex requests, use a file:

```bash
# Create tool from JSON file
sbs create-tool --body=@tool-definition.json

# Update skill from YAML file
sbs update-skill <skill-name> --body=@skill-update.yaml
```

### Verbose Mode

See detailed request/response information:

```bash
sbs list-tools -v
```

## Architecture

`sbs` is a single Go executable that links restish in as a library. There is no subprocess, nothing to find on `PATH`, and nothing unpacked at startup.

The source lives in `client/go`:

| Path | Contents |
| --- | --- |
| `client/go/cli/` | The implementation (`package cli`) |
| `client/go/cli/cmd/sbs/` | The binary's entry point (`package main`) |
| `client/go/tests/` | The Go test suite |
| `client/go/build.sh` | Cross-compiles every platform (`make cli-dist`) |

Build it with `make cli-build` (this platform) or `make cli-dist` (all five). The
library/entry-point split is required rather than stylistic: Go cannot import a
`package main`, so the tests can only live in their own directory if the
implementation is an importable package.

1. **Compiled-in target**: the binary carries the store URL it was built or prepared for. Resolution order is user config (`sbs connect`) → `SBS_URL` → the compiled-in URL.

2. **Generated commands**: on first use it fetches `/openapi.json` and caches it under `~/.cache/sbs/specs`. Operations appear as root commands (`sbs list-skills`), so the command surface tracks the store automatically.

3. **On-demand authentication**: requests go out unauthenticated first. If the store answers `401`, the CLI prompts once, exchanges the credentials at `POST /auth/login`, and caches the bearer token in `~/.config/sbs/tokens.cbor`. Public endpoints never prompt, and credentials never appear on the command line.

4. **Support commands** live under `sbs cli`: `sbs cli doctor`, `sbs cli config path`, `sbs cli cache clear`, `sbs cli auth inspect`.

### Environment variables

| Variable | Meaning |
| --- | --- |
| `SBS_URL` | Override the store URL for one invocation |
| `SBS_TOKEN` | Use a bearer token instead of prompting (CI). Select with `-p env-token` |

## Troubleshooting

### CLI not found after installation

Put the binary somewhere on your PATH, or add its directory:

```bash
# Linux/macOS
mv sbs ~/.local/bin/ && export PATH="$HOME/.local/bin:$PATH"

# Windows
# Add the directory holding sbs.exe to your PATH
```

### `pip install skillberry-store-sdk` no longer gives me `sbs`

That is deliberate. Install `skillberry-store-cli` (or `skillberry-store-sdk[cli]`), or download the binary from a store — see [Installation](#installation).

### "could not load the API description from ..."

The CLI could not reach the store's `/openapi.json`. It prints the URL it tried. Either the store is not running, or the binary is pointed at the wrong one:

```bash
sbs connect http://localhost:8000     # repoint permanently
SBS_URL=http://localhost:8000 sbs list-skills   # just this once
sbs cli doctor                        # show what is currently configured
```

`sbs connect` and `sbs download-cli` work even when the store is unreachable — they do not need the spec.

### Connection refused

Ensure the Skillberry Store service is running:

```bash
# Check if service is running
curl http://localhost:8000/docs

# Start the service
make run
```

### API spec sync failed

Clear the cached spec and re-run any command:

```bash
sbs cli cache clear
sbs --help
```

### `sbs` mentions "Restish" in a couple of flag descriptions

Two inherited global flag descriptions (`--help-all`, `--rsh-config`) and `sbs cli doctor`'s version label still name the embedded engine. They have no configuration hook upstream yet; a patch is in progress. Everything else — usage, examples, errors, hints and paths — says `sbs`.

## Examples

### Complete Workflow Example

```bash
# 1. Connect to your server
sbs connect http://localhost:8000

# 2. List existing tools
sbs list-tools

# 3. Add a new tool
sbs create-tool --name calculator --description "Basic calculator"

# 4. Execute the tool
sbs execute-tool calculator --body='{"params":{"x":5,"y":3}}'

# 5. Create a skill using the tool
sbs create-skill --name math-skill --description "Math utilities"

# 6. List skills to verify
sbs list-skills
```

### Batch Operations

```bash
# Export all tools
sbs list-tools -o json > tools-backup.json

# Import tools from backup
cat tools-backup.json | jq -c '.[]' | while read tool; do
  sbs create-tool --body="$tool"
done
```

## Install skills into your agent with npx

A skill in the store can be installed straight into Claude Code, Cursor, Codex or
any of ~70 other agents with one command and no prior setup — no `npm install`,
no `skills` CLI on your PATH, no git credentials, no login from the terminal:

```bash
npx skills add https://store.example.com/pub/pdf-forms -y -a claude-code
```

`npx` fetches the [`skills`](https://www.npmjs.com/package/skills) CLI on first
use and caches it. The store serves the skill's bytes directly over two
read-only `GET` endpoints, using the open
`/.well-known/agent-skills/index.json` discovery convention. Those two endpoints
exist for npx only: they are excluded from `/openapi.json`, so neither the
generated SDK nor `sbs` has a method or command for them.

**You do not have to compose that command.** Every surface hands you the whole
thing, flags included:

```bash
# one skill — either spelling works
sbs get-skill pdf-forms --npx-agent claude-code
sbs get-skill pdf-forms --fields name,_npx_install

# every skill you can see, each with its own command
sbs list-skills --fields name,_npx_install
```

`--npx-agent` both picks the agent and asks for the command, since it has no
other effect. Note that **no preset returns the command — `--fields full`
included**: it is a capability URL, so it is opt-in only, by one of the two
spellings above (see [§4.3.5](design/npx.md)). `grep npx` over a `--fields full`
response finding nothing is the designed behaviour, not a missing field.

In the UI, each skill's detail page has an **Install with npx** card with an
agent picker and a copy button.

### Installing several skills

There is deliberately one skill per install URL, so bulk installs are scripted
rather than served by a single aggregate URL:

```bash
sbs list-skills --fields _npx_install -o json \
  | jq -r '.[]._npx_install' \
  | while read -r cmd; do eval "$cmd"; done
```

Each `npx` run re-does discovery, but the npm package is cached after the first,
so this is seconds per skill.

### Keeping up to date

```bash
npx skills update
```

Re-fetches the index and re-downloads only the skills whose content changed. A
store-side edit reaches you on your next `update`; a skill deleted from the store
is offered for removal.

### What the flags are for

Both matter, and both are emitted for you — they are not decoration:

| Flag | Why |
| --- | --- |
| `-a <agent>` | `-y` with no detected agent installs the skill into **every** supported agent's directory — around 75 of them in your project tree. |
| `-y` | Skips the scope, symlink and confirmation prompts. |

### Telemetry, and how to opt out

On a **successful** install (not on discovery, and not on `--list`) the CLI sends
a request to `add-skill.vercel.sh` carrying your hostname, the skill name, the
artifact URL, and `installUrl` — the URL you typed, which on a secured store
contains an access token. **File contents are never sent.** The CLI's own
privacy-suppression path only understands GitHub repository visibility, so it has
no concept of a private well-known host and reports an intranet store like a
public one.

Opt out once, for every run:

```bash
# bash / zsh — in ~/.bashrc, ~/.zshrc, or your CI environment
export DO_NOT_TRACK=1
```

```powershell
# PowerShell — in $PROFILE
$env:DO_NOT_TRACK = '1'
```

`DISABLE_TELEMETRY=1` has the same effect; `DO_NOT_TRACK` is the
[cross-vendor convention](https://consoledonottrack.com/), so one export also
covers the other CLIs that honour it.

The install command the store emits deliberately carries **no** environment-variable
prefix, for three reasons:

- a prefix protects exactly one invocation — every later `npx skills update` you
  type yourself would be unaffected, so the opt-out has to live in your
  environment to mean anything;
- `VAR=1 command` is POSIX shell syntax, so a prefixed command would simply fail
  in PowerShell and `cmd.exe`;
- suppressing by default would quietly remove every SBS install from the
  ecosystem's install counts, which is not a decision a store should make on its
  users' behalf without saying so.

The trade-off is explicit: the default reports the install, and on a secured store
that report includes the token. If that is unacceptable for your deployment, set
`DO_NOT_TRACK=1` in the environment your users' shells inherit — and note that
the UI already says this next to the copy button, where someone about to paste a
credential-bearing URL will see it.

### Operator notes

npx publishing is controlled by `npx_publish` in `access_control_config.yaml`.
It is declared there rather than in an environment variable because it *is* an
access-control decision: it is the one setting that makes skill **content**, not
just metadata, readable without a session.

It has three values, and is **independent of the `mode:` above** — every
combination is legal, because a mode-dependent default is exactly the subtlety
that surprises whoever later enables auth:

| `npx_publish` | Effect |
| --- | --- |
| `true` | Every visible skill is publishable. Per-skill flags are ignored. |
| `false` | Nothing is; the `/pub` routes are not registered at all. Per-skill flags are ignored. |
| `selective` | **The default.** Each skill's own `npx_publish` flag decides. |

`true` and `false` deliberately override the per-skill flag, so an operator can
publish or withdraw the whole store in one edit and be certain that is what
happened.

Under `selective`, a skill that has **not** set its flag is **not** published, so
a store that has configured nothing publishes nothing. Opt a skill in by editing
it — it is an ordinary manifest field:

```bash
sbs update-skill pdf-forms --body='{"name":"pdf-forms","description":"…","npx_publish":true}'
```

…or with the **Publish this skill with npx** switch on the skill's page in the UI,
in its *Install with npx* card. Changing it needs the same `skills:update`
permission as any other manifest edit; no separate role or endpoint is involved.
A junk value for the store-wide setting fails **closed** (`false`) with a warning,
rather than silently landing on the default.

The switch always shows the **effective** state, and is greyed out when it is not
what decides:

| Store-wide `npx_publish` | Switch reads | Editable | Hover says |
| --- | --- | --- | --- |
| `true` | `…: YES` | no | "npx publish enabled globally" |
| `false` | `…: NO` | no | "npx publish disabled globally" |
| `selective` | the skill's own flag | yes, with `skills:update` | — |

Without `skills:update` the switch is locked whatever the mode, and says so on
hover. The label spells the state out rather than relying on the switch position,
which is hard to read when the control is greyed out. **When it reads `NO` the card
is just the switch** — no agent picker, no command, no notes, because there is
nothing to install.

That distinction matters: on a store set to `true` a skill's own flag is ignored,
so a switch rendering the raw flag would read "off" beside a working install
command. Two computed read-only fields carry the context a client needs —
`npx_publish_mode` (the store-wide value) and `npx_publish_editable` (whether this
caller holds `skills:update`) — and both arrive with any preset, unlike the
opt-in-only `_npx_install`.

When the switch reads off, there is no install command **and the install URL stops
resolving**: the index and the archive both return 404, so a URL someone kept from
earlier is refused rather than merely hidden.

| Concern | What to know |
| --- | --- |
| **What gets published** | Under `true`, every skill visible to a holder of `skills:list`; under `selective`, those of them that have opted in. One skill per install URL. There is still no lifecycle-state or tag filter: a `state: new` draft is already visible to every such user, so hiding it from npx would misreport what the store contains. The per-skill flag is a deliberate, visible, editable opt-in — not an invisible server-side rule. |
| **Checking what is published** | The boot log states the mode and how many skills are publishable right now, so `selective` with nothing opted in reads as `0` rather than being discovered through a failed install. |
| **`SBS_PUBLIC_URL`** | Set it. The install command is absolute, and behind an ingress or load balancer the server cannot derive its own externally-visible URL; without it, the command is omitted rather than guessed. Useful beyond npx — it is the value any copy-paste snippet needs. |
| **Access tokens in URLs** | With access control on, the path segment is a read-only capability token scoped to **one skill**, derived from a durable seed (`SBS_PUBLISH_SEED`, which must be treated as confidential — anyone holding it can derive a URL for any skill without authenticating). It is re-authorized on every request, so it stops working the moment its tenant loses `skills:list` or its account is removed. It is not accepted as an `Authorization: Bearer` value anywhere. Note that it travels to `add-skill.vercel.sh` in `installUrl` unless `DO_NOT_TRACK=1` is set — see above. |
| **The token is durable** | It keeps returning *future* edits to that skill, not only the version installed, until the secret is rotated. Where the skill's content is committed next to the lockfile that is immaterial; it matters if a published skill later gains sensitive content. |
| **Revoking** | Losing `skills:list` revokes automatically, per request. To revoke everything at once, rotate `SBS_PUBLISH_SEED` — that is the intended global revoke, and everyone then re-copies their command. |
| **Committing `skills-lock.json`** | Safe. A project install writes the lockfile *and* the skill's own files in the same commit, so a per-skill token in it grants read access to content that is already in the repository beside it. |
| **Alternatives to a URL-borne token** | Running with access control disabled behind a network boundary (the URL is then just the skill's slug, with no secret), terminating auth at a gateway in front of the store, or leaving npx publishing off entirely and using the UI, the `sbs` CLI or vNFS. All three are supported configurations, not failure modes. |
| **Hiding one skill** | Tag it `npx-internal`. Its own install URL keeps working — it has to, or the command on its page would break — but its emitted frontmatter carries `metadata.internal: true`, which the CLI honours by leaving it out of a multi-skill install list unless `INSTALL_INTERNAL_SKILLS=1`. |
| **Namespace packs** | A namespace-scoped install URL publishes one namespace, which is the analogue of a skills.sh "pack". Set `SBS_PUBLISH_NAMESPACES` to a comma-separated allowlist to restrict which namespaces may be named that way; per-skill URLs are unaffected. |
| **Renaming a skill** | Changes its slug, so it leaves the index under the old name and reappears under the new one; an already-installed copy is orphaned. Expected, not a defect. |
| **Version chains** | An install URL always installs the HEAD. On a chain with no unambiguous HEAD, which object is published follows the same resolution as `GET /skills/{name}` — debug the chain, not the index. |

See [docs/design/npx.md](design/npx.md) for the protocol findings this is built
on, including what the CLI validates and what it silently drops.

## Integration with Scripts

The CLI can be easily integrated into shell scripts:

```bash
#!/bin/bash

# Check if tool exists
if sbs get-tool my-tool 2>/dev/null; then
  echo "Tool exists"
else
  echo "Creating tool..."
  sbs create-tool --body=@tool.json
fi

# Get tool output as JSON
RESULT=$(sbs execute-tool my-tool --body='{"params":{}}' -o json)
echo "Result: $RESULT"
```

## Related Documentation

- [Skillberry Store API Documentation](http://localhost:8000/docs)
- [Restish Documentation](https://rest.sh/)
- [Python SDK Documentation](https://github.com/skillberry-ai/skillberry-store-sdk)
- [Configuration Guide](config-env-vars.md)

## Support

For issues or questions:
- Check the [main README](../README.md)
- Review [API documentation](http://localhost:8000/docs)
- Contact the development team
