// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0

/**
 * CliDownloadModal — docs/design/new_cli.md §8.3 #21.
 *
 * The assertions that matter are the ones a user notices when they break: the
 * detected platform is preselected, every platform stays selectable, the digest
 * is reachable but not cluttering the primary action, `preparing` reads as "come
 * back" rather than an error, a failure is visible rather than a blank modal, and
 * the download control is a real `<a href download>` carrying the right query
 * string (not a Button whose onClick would buffer 32 MB into memory).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CliDownloadModal } from './CliDownloadModal';

const SHA = 'a'.repeat(64);

/** A HEAD response from the download endpoint. */
function headOk(overrides: Record<string, string> = {}) {
  const headers = new Headers({
    'X-SBS-SHA256': SHA,
    'X-SBS-CLI-Version': '1.2.3',
    'X-SBS-CLI-URL-Injection': 'patch',
    'Content-Length': '33000000',
    ...overrides,
  });
  return { ok: true, status: 200, headers };
}

function headStatus(status: number, headers: Record<string, string> = {}) {
  return { ok: false, status, headers: new Headers(headers) };
}

function setUserAgent(ua: string) {
  Object.defineProperty(window.navigator, 'userAgent', {
    value: ua,
    configurable: true,
  });
}

const UA_LINUX = 'Mozilla/5.0 (X11; Linux x86_64) Chrome/123.0.0.0';
const UA_MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15';
const UA_WINDOWS = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0';

describe('CliDownloadModal', () => {
  beforeEach(() => {
    setUserAgent(UA_LINUX);
    // No userAgentData by default: that is Safari and Firefox, and the path where
    // the User-Agent guess has to stand on its own.
    delete (window.navigator as any).userAgentData;
    global.fetch = vi.fn().mockResolvedValue(headOk()) as any;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('inspects the selected platform with HEAD, not GET', async () => {
    // The point of HEAD: learn the digest and size without pulling ~32 MB.
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as any).mock.calls[0];
    expect(init?.method).toBe('HEAD');
    expect(url).toContain('/cli/download?platform=');
    expect(url).toContain('format=raw');
  });

  it('does not touch the network while closed', () => {
    render(<CliDownloadModal isOpen={false} onClose={() => {}} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('preselects the platform detected from the User-Agent', async () => {
    setUserAgent(UA_WINDOWS);
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    const select = (await screen.findByLabelText(
      'Select your platform'
    )) as HTMLSelectElement;
    expect(select.value).toBe('windows-amd64');
  });

  it('prefers Apple Silicon for a Mac User-Agent', async () => {
    // Every Mac reports "Intel Mac OS X 10_15_7", so the UA cannot reveal the
    // CPU. Apple Silicon is the more likely machine, and the chooser stays
    // visible precisely because this is a guess.
    setUserAgent(UA_MAC);
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    const select = (await screen.findByLabelText(
      'Select your platform'
    )) as HTMLSelectElement;
    expect(select.value).toBe('darwin-arm64');
  });

  it('refines a Mac guess with userAgentData when available', async () => {
    // The only client-side way to learn a Mac's real architecture. An Intel Mac
    // must end up on darwin-amd64 despite the UA guess.
    setUserAgent(UA_MAC);
    (window.navigator as any).userAgentData = {
      getHighEntropyValues: vi.fn().mockResolvedValue({
        platform: 'macOS',
        architecture: 'x86',
        bitness: '64',
      }),
    };

    render(<CliDownloadModal isOpen onClose={() => {}} />);

    await waitFor(async () => {
      const select = (await screen.findByLabelText(
        'Select your platform'
      )) as HTMLSelectElement;
      expect(select.value).toBe('darwin-amd64');
    });
  });

  it('keeps the guess when userAgentData is rejected', async () => {
    // Some privacy configurations refuse the request; the guess must survive.
    setUserAgent(UA_MAC);
    (window.navigator as any).userAgentData = {
      getHighEntropyValues: vi.fn().mockRejectedValue(new Error('denied')),
    };

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    const select = (await screen.findByLabelText(
      'Select your platform'
    )) as HTMLSelectElement;
    expect(select.value).toBe('darwin-arm64');
  });

  it('offers every platform, none hidden behind the detection', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    const select = await screen.findByLabelText('Select your platform');
    const options = within(select).getAllByRole('option') as HTMLOptionElement[];

    expect(options.map((o) => o.value).sort()).toEqual(
      [
        'darwin-amd64',
        'darwin-arm64',
        'linux-amd64',
        'linux-arm64',
        'windows-amd64',
      ].sort()
    );
  });

  it('keeps the digest behind a disclosure rather than beside the button', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    // Collapsed until asked for: 64 hex characters next to the primary action is
    // clutter for everyone who will not check it. PatternFly renders the content
    // with `hidden` rather than omitting it, so the assertion is on the
    // disclosure state, not on the digest's absence from the DOM.
    await screen.findByRole('link', { name: /Download sbs/i });
    const toggle = await screen.findByRole('button', {
      name: /Verify this download/i,
    });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.getByDisplayValue(SHA).closest('[hidden]')).not.toBeNull();

    await userEvent.click(toggle);

    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    // ClipboardCopy renders its content in a read-only input, not as text.
    expect(screen.getByDisplayValue(SHA).closest('[hidden]')).toBeNull();
  });

  it('shows a command whose output the digest can be compared against', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await userEvent.click(
      await screen.findByRole('button', { name: /Verify this download/i })
    );
    expect(await screen.findByDisplayValue(/shasum -a 256 sbs/)).toBeTruthy();
  });

  it('uses the PowerShell hash command for Windows', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);
    const select = await screen.findByLabelText('Select your platform');
    await userEvent.selectOptions(select, 'windows-amd64');

    await userEvent.click(
      await screen.findByRole('button', { name: /Verify this download/i })
    );
    expect(await screen.findByDisplayValue(/Get-FileHash.*sbs\.exe/)).toBeTruthy();
  });

  it('offers the download as an <a href download> with the right query string', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    const link = (await screen.findByRole('link', {
      name: /Download sbs/i,
    })) as HTMLAnchorElement;

    // An anchor, not a button: the browser must own a 32 MB transfer.
    expect(link.tagName).toBe('A');
    expect(link.hasAttribute('download')).toBe(true);
    expect(link.getAttribute('href')).toContain(
      '/cli/download?platform=linux-amd64&format=raw'
    );
  });

  it('offers the archive download separately', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);
    const link = (await screen.findByRole('link', {
      name: /Download archive/i,
    })) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('format=archive');
  });

  it('re-inspects and repoints the link when the platform changes', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await screen.findByRole('link', { name: /Download sbs/i });

    const select = await screen.findByLabelText('Select your platform');
    await userEvent.selectOptions(select, 'darwin-arm64');

    await waitFor(() => {
      const link = screen.getByRole('link', {
        name: /Download sbs/i,
      }) as HTMLAnchorElement;
      expect(link.getAttribute('href')).toContain('platform=darwin-arm64');
    });
    // The digest is per-artifact, so a platform change must re-read it.
    const urls = (global.fetch as any).mock.calls.map((c: any[]) => c[0]);
    expect(urls.some((u: string) => u.includes('platform=darwin-arm64'))).toBe(true);
  });

  it('explains the sidecar mechanism for platforms that need it', async () => {
    // darwin-arm64 carries its URL beside the binary, so a raw download alone
    // would come up pointing at the compile-time default.
    global.fetch = vi
      .fn()
      .mockResolvedValue(headOk({ 'X-SBS-CLI-URL-Injection': 'sidecar' })) as any;

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/separate file/i)).toBeTruthy();
    });
  });

  it('reads "being prepared" from a 503, not as an error', async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(headStatus(503, { 'Retry-After': '10' })) as any;

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/being prepared/i)).toBeTruthy();
    });

    // And the download is not offered as if it would work. A disabled anchor
    // carries no href, so it has no link role — hence the text query.
    const control = screen.getByText(/Download sbs/i).closest('a');
    expect(control?.getAttribute('aria-disabled')).toBe('true');
    expect(control?.hasAttribute('href')).toBe(false);
  });

  it('reads "not available" from a 404 and names the alternative', async () => {
    global.fetch = vi.fn().mockResolvedValue(headStatus(404)) as any;

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Not available from this store/i)).toBeTruthy();
    });
    expect(screen.getByText(/skillberry-store-cli/)).toBeTruthy();
  });

  it('shows an inline alert on a network failure, never a blank modal', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('network down')) as any;

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Could not reach the store/i)).toBeTruthy();
    });
  });

  it('can retry after a failure', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue(headOk());
    global.fetch = fetchMock as any;

    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await screen.findByText(/Could not reach the store/i);

    await userEvent.click(screen.getByRole('button', { name: /Try again/i }));

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /Download sbs/i })).toBeTruthy();
    });
  });

  it('warns that the binaries are unsigned', async () => {
    render(<CliDownloadModal isOpen onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/not code-signed/i)).toBeTruthy();
    });
  });

  it('sends no credentials with the inspect request', async () => {
    // The modal is offered on the sign-in screen, so it must work with no
    // session. Credentials would also fail CORS in some deployments.
    render(<CliDownloadModal isOpen onClose={() => {}} />);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [, init] = (global.fetch as any).mock.calls[0];
    expect(init?.credentials).toBeUndefined();
    expect((init?.headers as any)?.Authorization).toBeUndefined();
  });

  it('calls onClose from the Close button', async () => {
    const onClose = vi.fn();
    render(<CliDownloadModal isOpen onClose={onClose} />);

    // PatternFly's Modal renders its own dismiss control also labelled "Close",
    // so match the footer action specifically rather than by name alone.
    const closeButtons = await screen.findAllByRole('button', { name: /^Close$/i });
    await userEvent.click(closeButtons[closeButtons.length - 1]);
    expect(onClose).toHaveBeenCalled();
  });
});
