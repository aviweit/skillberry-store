// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0
//
// The CLI download link on the sign-in screen.
// docs/design/new_cli.md §5.9 and §8.3 #21b.
//
// This is a decision the design settled rather than deferred: the sign-in screen
// DOES offer the download, because a user who cannot sign in yet is exactly the
// user who wants the CLI, and `/cli/*` is unauthenticated by construction so the
// modal works pre-session with no special case.
//
// Two placement rules matter and are asserted, not just described:
//
//  * the link sits BELOW the sign-in card body, so it never competes with the
//    form for attention;
//  * it sits BENEATH any LoginBanner, so an operator's message stays the first
//    thing read.

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { LoginPage } from './LoginPage';

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    mode: 'standalone' as const,
    token: null,
    tenantId: null,
    signIn: vi.fn(),
    signOut: vi.fn(),
    whoami: vi.fn(),
  }),
}));

const LOGIN_INFO_META = 'sbs-login-info';

/** What a HEAD on /cli/download reports for a ready artifact. */
const HEAD_OK = {
  ok: true,
  status: 200,
  headers: new Headers({
    'X-SBS-SHA256': 'a'.repeat(64),
    'X-SBS-CLI-Version': '1.2.3',
    'X-SBS-CLI-URL-Injection': 'patch',
    'Content-Length': '33000000',
  }),
};

function renderLoginPage() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>
  );
}

beforeEach(() => {
  Object.defineProperty(window.navigator, 'userAgent', {
    value: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/123.0.0.0',
    configurable: true,
  });
  delete (window.navigator as any).userAgentData;
  global.fetch = vi.fn().mockResolvedValue(HEAD_OK) as any;
});

afterEach(() => {
  document.head
    .querySelectorAll(`meta[name="${LOGIN_INFO_META}"]`)
    .forEach((el) => el.remove());
  vi.restoreAllMocks();
});

describe('LoginPage CLI download link', () => {
  it('renders unauthenticated, with no session', () => {
    // The whole point: this is rendered by a user who has no token at all.
    renderLoginPage();
    expect(screen.getByRole('button', { name: 'Download CLI' })).toBeTruthy();
  });

  it('shows only the download icon, with no visible caption', () => {
    // Uniformity with the masthead's control, which is icon-only and is the
    // only one that exists once signed in or in `mode: disabled`. The label
    // lives in aria-label, so the control stays reachable without it.
    renderLoginPage();
    const link = screen.getByRole('button', { name: 'Download CLI' });
    expect(link.textContent).toBe('');
  });

  it('does not touch the network until the link is clicked', () => {
    // The sign-in screen must not pay for a feature most visitors will not use.
    renderLoginPage();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('opens the download modal', async () => {
    renderLoginPage();
    await userEvent.click(
      screen.getByRole('button', { name: 'Download CLI' })
    );

    await waitFor(() => {
      expect(screen.getByLabelText('Select your platform')).toBeTruthy();
    });
    const [url, init] = (global.fetch as any).mock.calls[0];
    expect(url).toContain('/cli/download?platform=');
    expect(init?.method).toBe('HEAD');
  });

  it("sends no credentials with the modal's request", async () => {
    // It has to work before a session exists, so credentials would be both
    // pointless and (in some deployments) a CORS failure.
    renderLoginPage();
    await userEvent.click(
      screen.getByRole('button', { name: 'Download CLI' })
    );

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [, init] = (global.fetch as any).mock.calls[0];
    expect(init?.credentials).toBeUndefined();
    expect(init?.headers?.Authorization).toBeUndefined();
  });

  it('sits below the sign-in form', () => {
    const { container } = renderLoginPage();
    const link = screen.getByRole('button', { name: 'Download CLI' });
    const form = container.querySelector('form');
    expect(form).toBeTruthy();

    // DOCUMENT_POSITION_FOLLOWING (4) means the link comes after the form.
    const relation = form!.compareDocumentPosition(link);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('sits beneath the operator login message', () => {
    // An operator's message is the first thing that should be read on this
    // screen; a download link must never push it down.
    const meta = document.createElement('meta');
    meta.setAttribute('name', LOGIN_INFO_META);
    meta.setAttribute('content', 'Shared eval box — do not store secrets.');
    document.head.appendChild(meta);

    renderLoginPage();

    const info = screen.getByText('Shared eval box — do not store secrets.');
    const link = screen.getByRole('button', { name: 'Download CLI' });
    const relation = info.compareDocumentPosition(link);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('still shows the sign-in form', () => {
    // A regression guard: the footer must not displace what this page is for.
    renderLoginPage();
    expect(screen.getByLabelText(/Username/i)).toBeTruthy();
    expect(screen.getByLabelText(/Password/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Sign in$/i })).toBeTruthy();
  });
});
