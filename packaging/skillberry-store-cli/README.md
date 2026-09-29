# skillberry-store-cli

The native `sbs` command-line interface for [Skillberry Store](https://github.com/skillberry-ai/skillberry-store).

```bash
pip install skillberry-store-cli
sbs --help
```

This is a **platform wheel carrying a prebuilt static binary** plus a three-line
launcher — the same pattern `ruff`, `uv` and `esbuild` use. There is no Python
implementation of the CLI: the binary is the same artifact a store serves at
`/cli/download`, so there is exactly one `sbs`.

## Pointing it at a store

A binary from `pip` has no store baked in, so tell it once:

```bash
sbs connect https://store.example.com
sbs list-skills
```

Or per command, with `SBS_URL=https://store.example.com`.

A binary downloaded **from a store** needs none of this — it already talks to
that store. If you have access to one, that is the better route:

```bash
curl -fsSL "https://store.example.com/cli/download?platform=linux-amd64" -o sbs
chmod +x sbs
```

## No wheel for your platform?

Wheels are published for `linux-amd64`, `linux-arm64`, `darwin-amd64`,
`darwin-arm64` and `windows-amd64`. Anywhere else, download from a store (above)
or build from source — `client/go` in the repository.

## Upgrading from `skillberry-store-sdk`

`pip install skillberry-store-sdk` used to provide an `sbs` script that required
a separate `restish` install. It no longer provides `sbs` at all. Install this
package instead, or `skillberry-store-sdk[cli]` for both. The SDK itself remains
a pure Python library that installs anywhere.

## Licence

Apache-2.0. The binary embeds [restish](https://rest.sh/) (MIT); its licence ships
alongside the executable inside the wheel, and is reproduced in the repository's
own LICENSE under *Third-party software notices*. `sbs --version` points at it.
