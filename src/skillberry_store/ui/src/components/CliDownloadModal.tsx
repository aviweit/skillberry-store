// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0

import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  ClipboardCopy,
  ExpandableSection,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Modal,
  ModalVariant,
  Spinner,
  Text,
} from '@patternfly/react-core';
import { DownloadIcon } from '@patternfly/react-icons';
import { cliApi } from '@/services/api';
import {
  CLI_PLATFORM_LABELS,
  type CliArtifact,
  type CliPlatform,
} from '@/types/cli';

/**
 * Download the native `sbs` CLI — docs/design/new_cli.md §5.9.
 *
 * One component, three entry points (masthead, home card, sign-in screen). Four
 * properties are deliberate:
 *
 * 1. **It works unauthenticated.** `/cli/download` is unauthenticated in every
 *    ACL mode, so this renders and functions on the sign-in screen with no
 *    special case — which matters because a user who cannot sign in yet is
 *    exactly the user who wants the CLI.
 *
 * 2. **The chooser is always visible, never hidden behind the detection.**
 *    Apple Silicon is undecidable from a User-Agent — every Mac reports "Intel
 *    Mac OS X 10_15_7" — so the guess can be wrong. The UI refines it with
 *    `navigator.userAgentData` where available and still shows every option,
 *    because silently handing an Intel Mac an arm64 binary produces a failure
 *    ("killed") that looks nothing like the cause.
 *
 * 3. **Download is a plain `<a href download>`, not a fetch.** The browser owns a
 *    32 MB transfer better than JavaScript: progress, resume and streaming to
 *    disk come free, whereas a blob buffers the whole artifact in memory.
 *
 * 4. **The checksum is behind a disclosure, not printed beside the button.** A
 *    64-character hex string next to the primary action is clutter for the many
 *    people who will not check it, so "Verify this download" reveals the digest
 *    and the one command that produces a comparable value.
 */

interface CliDownloadModalProps {
  isOpen: boolean;
  onClose: () => void;
}

/** Bytes → a short human string for the download button. */
function formatSize(bytes?: number): string {
  if (!bytes) return '';
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(1)} MB`;
}

/**
 * Refine the platform guess using UA Client Hints.
 *
 * `navigator.userAgentData.getHighEntropyValues` is the only client-side way to
 * learn a Mac's real architecture. Chromium-only and async; everything else keeps
 * the User-Agent guess.
 */
async function detectPlatformClientSide(): Promise<CliPlatform | null> {
  const uaData = (navigator as any).userAgentData;
  if (!uaData?.getHighEntropyValues) return null;
  try {
    const { platform, architecture, bitness } = await uaData.getHighEntropyValues([
      'platform',
      'architecture',
      'bitness',
    ]);
    if (bitness && bitness !== '64') return null;
    const arch = architecture === 'arm' ? 'arm64' : architecture === 'x86' ? 'amd64' : null;
    if (!arch) return null;
    if (platform === 'macOS') return `darwin-${arch}` as CliPlatform;
    if (platform === 'Windows') return arch === 'amd64' ? 'windows-amd64' : null;
    if (platform === 'Linux' || platform === 'Chrome OS') return `linux-${arch}` as CliPlatform;
    return null;
  } catch {
    // Some privacy configurations reject the request outright. The User-Agent
    // guess is a perfectly good fallback.
    return null;
  }
}

/** Best guess from the User-Agent, used only to preselect an option. */
function guessFromUserAgent(): CliPlatform {
  const ua = navigator.userAgent || '';
  if (/windows/i.test(ua)) return 'windows-amd64';
  if (/mac os x|macintosh/i.test(ua)) return 'darwin-arm64';
  if (/aarch64|arm64/i.test(ua)) return 'linux-arm64';
  return 'linux-amd64';
}

/** The command whose output the displayed digest should equal. */
function verifyCommand(platform: CliPlatform, filename: string): string {
  return platform.startsWith('windows-')
    ? `Get-FileHash -Algorithm SHA256 ${filename}`
    : `shasum -a 256 ${filename}`;
}

function artifactFilename(platform: CliPlatform): string {
  return platform.startsWith('windows-') ? 'sbs.exe' : 'sbs';
}

export function CliDownloadModal({ isOpen, onClose }: CliDownloadModalProps) {
  const [selected, setSelected] = useState<CliPlatform>(guessFromUserAgent());
  const [artifact, setArtifact] = useState<CliArtifact | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const inspect = useCallback(async (platform: CliPlatform) => {
    setIsLoading(true);
    setError(null);
    setUnavailable(false);
    setArtifact(null);
    try {
      const info = await cliApi.inspect(platform);
      if (info === null) {
        setUnavailable(true);
      } else {
        setArtifact(info);
      }
    } catch (e) {
      // An inline Alert, never a blank modal: "nothing happened" is the worst
      // possible answer to a click on Download.
      setError(e instanceof Error ? e.message : 'Could not reach the store');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    void detectPlatformClientSide().then((detected) => {
      if (detected) setSelected(detected);
    });
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    void inspect(selected);
  }, [isOpen, selected, inspect]);

  const isReady = artifact?.state === 'ready';
  const isPreparing = artifact?.state === 'preparing';
  const filename = artifactFilename(selected);
  // Only handed to the anchors when the artifact is actually downloadable:
  // PatternFly keeps `href` on a disabled anchor Button, so a "disabled" control
  // with an href would still be clickable and would land the user on a 503.
  const rawHref = isReady ? cliApi.url(selected, 'raw') : undefined;
  const archiveHref = isReady ? cliApi.url(selected, 'archive') : undefined;

  return (
    <Modal
      variant={ModalVariant.medium}
      title="Download the sbs CLI"
      isOpen={isOpen}
      onClose={onClose}
      actions={[
        // Real anchors, not Buttons with onClick: the browser owns the transfer,
        // so progress, resume and streaming to disk all come free. A fetch into a
        // blob would buffer the whole ~32 MB artifact in memory.
        <Button
          key="download"
          variant="primary"
          component="a"
          href={rawHref}
          download
          isDisabled={!isReady}
          icon={<DownloadIcon />}
        >
          Download {filename}
          {artifact?.size ? ` (${formatSize(artifact.size)})` : ''}
        </Button>,
        <Button
          key="archive"
          variant="secondary"
          component="a"
          href={archiveHref}
          download
          isDisabled={!isReady}
        >
          Download archive
        </Button>,
        <Button key="close" variant="link" onClick={onClose}>
          Close
        </Button>,
      ]}
    >
      <>
        <Text component="p">
          A single native executable — no Python, no <code>pip</code>, nothing else to
          install. This build talks to <strong>{window.location.origin}</strong> with no
          configuration.
        </Text>

        <Form>
          <FormGroup label="Platform" fieldId="cli-platform">
            <FormSelect
              id="cli-platform"
              value={selected}
              onChange={(_e, value) => setSelected(value as CliPlatform)}
              aria-label="Select your platform"
            >
              {CLI_PLATFORM_LABELS.map(({ id, label }) => (
                <FormSelectOption key={id} value={id} label={label} />
              ))}
            </FormSelect>
          </FormGroup>
        </Form>

        {isLoading && (
          <div style={{ padding: '1rem 0' }}>
            <Spinner size="md" aria-label="Checking this platform's build" />
          </div>
        )}

        {error && (
          <Alert
            variant="danger"
            isInline
            title="Could not reach the store"
            style={{ marginTop: '1rem' }}
            actionLinks={
              <Button variant="link" isInline onClick={() => void inspect(selected)}>
                Try again
              </Button>
            }
          >
            {error}
          </Alert>
        )}

        {unavailable && (
          <Alert
            variant="warning"
            isInline
            title="Not available from this store"
            style={{ marginTop: '1rem' }}
          >
            This store has no build for {selected}. Install it with{' '}
            <code>pip install skillberry-store-cli</code>, or pick another platform.
          </Alert>
        )}

        {isPreparing && (
          <Alert
            variant="info"
            isInline
            title="This build is being prepared"
            style={{ marginTop: '1rem' }}
            actionLinks={
              <Button variant="link" isInline onClick={() => void inspect(selected)}>
                Refresh
              </Button>
            }
          >
            The store is embedding its URL into this platform's binary. Try again
            shortly.
          </Alert>
        )}

        {isReady && artifact?.urlInjection === 'sidecar' && (
          <Alert
            variant="info"
            isInline
            title="Download the archive for this platform"
            style={{ marginTop: '1rem' }}
          >
            Builds for this platform carry the store URL in a separate file rather than
            inside the binary, so the archive is the complete download. A bare binary
            works too, after <code>sbs connect {window.location.origin}</code>.
          </Alert>
        )}

        {isReady && artifact?.sha256 && (
          // Collapsed by default: a 64-character hex string beside the primary
          // action is clutter for everyone who will not check it, and a nuisance
          // to scroll past.
          <ExpandableSection
            toggleText="Verify this download"
            style={{ marginTop: '1rem' }}
          >
            <Text component="p">
              Run this against the file you downloaded and compare the result with the
              value below.
            </Text>
            <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied">
              {verifyCommand(selected, filename)}
            </ClipboardCopy>
            <Text component="p" style={{ marginTop: '0.5rem' }}>
              Expected sha256:
            </Text>
            <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied">
              {artifact.sha256}
            </ClipboardCopy>
          </ExpandableSection>
        )}

        <Text component="p" style={{ marginTop: '1.5rem', fontSize: '0.875rem' }}>
          {artifact?.version ? `sbs ${artifact.version} · ` : ''}
          The binaries are not code-signed: macOS may quarantine a browser download,
          and Windows SmartScreen may warn.
        </Text>
      </>
    </Modal>
  );
}
