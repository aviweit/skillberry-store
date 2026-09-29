// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0

/**
 * Types for the CLI download endpoint (`GET`/`HEAD` /cli/download).
 *
 * See docs/design/new_cli.md §5.5. There is one endpoint; which artifact you get
 * is a query argument, and an artifact's identity comes back in response headers
 * rather than in a document.
 */

/** Artifact platform ids. A closed enum server-side (§5.1). */
export type CliPlatform =
  | 'linux-amd64'
  | 'linux-arm64'
  | 'darwin-amd64'
  | 'darwin-arm64'
  | 'windows-amd64';

/** `raw` is the bare executable; `archive` preserves the executable bit. */
export type CliFormat = 'raw' | 'archive';

/**
 * How the store injected its URL into this artifact.
 *
 * `sidecar` matters to the UI: that binary carries no baked URL, so the archive
 * is the complete download for it and a raw download needs `sbs connect`.
 */
export type CliUrlInjection = 'patch' | 'rebuild' | 'sidecar' | 'pristine';

/** What a HEAD on the download endpoint reports. */
export interface CliArtifact {
  platform: CliPlatform;
  state: 'ready' | 'preparing';
  /** Hex sha256, present when ready. Compare against `sha256sum` output. */
  sha256?: string;
  size?: number;
  version?: string;
  urlInjection?: CliUrlInjection;
  /** Seconds to wait before retrying, when preparing. */
  retryAfter?: number;
}

/** Display order and labels for the platform chooser. */
export const CLI_PLATFORM_LABELS: Array<{ id: CliPlatform; label: string }> = [
  { id: 'darwin-arm64', label: 'macOS — Apple Silicon (M1–M4)' },
  { id: 'darwin-amd64', label: 'macOS — Intel' },
  { id: 'linux-amd64', label: 'Linux — x86-64' },
  { id: 'linux-arm64', label: 'Linux — ARM64' },
  { id: 'windows-amd64', label: 'Windows — x86-64' },
];
