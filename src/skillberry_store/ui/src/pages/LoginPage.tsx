// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0

import { FormEvent, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  Bullseye,
  Button,
  Card,
  CardBody,
  CardFooter,
  CardTitle,
  Form,
  FormGroup,
  TextInput,
  Alert,
  Stack,
  StackItem,
} from '@patternfly/react-core';
import { DownloadIcon } from '@patternfly/react-icons';
import { useAuth } from '@/contexts/AuthContext';
import { CliDownloadModal } from '@/components/CliDownloadModal';
import { LoginBanner } from '@/components/LoginBanner';
import { readBannerFromDocument } from '@/types/loginBanner';

export function LoginPage() {
  const { signIn, mode, token } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [isCliModalOpen, setIsCliModalOpen] = useState(false);
  // Operator-configured login message, injected into index.html by the server
  // (see §6 of docs/design/login-info.md). Read once in a state initializer:
  // the DOM value is fixed for the page's lifetime because the config is only
  // re-read on a server restart. Note that `make ui-dev` serves Vite's own
  // index.html and so never carries the tag.
  const [loginInfo] = useState<string | null>(
    () =>
      document
        .querySelector('meta[name="sbs-login-info"]')
        ?.getAttribute('content') || null
  );
  // The rich banner (`format: rich`), from its own tag and read the same way.
  // Null covers every "not rich, or not usable" case — no tag, malformed JSON,
  // an unknown payload version — and the plain Alert below then renders
  // instead, so a login screen always appears. See docs/design/login-banner.md.
  const [loginBanner] = useState(() => readBannerFromDocument());

  // Already signed in: redirect out of the login page.
  if (mode === 'disabled' || token) {
    navigate('/', { replace: true });
    return null;
  }

  const from = (location.state as { from?: string } | null)?.from || '/';

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn(username, password);
      navigate(from, { replace: true });
    } catch (err) {
      setError((err as Error).message || 'invalid_credentials');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Bullseye style={{ minHeight: '100vh', padding: '2rem' }}>
      {/* A rich banner gets a wider card: headings and pills need the room, and
          a banner squeezed into the form's width stops looking deliberate. */}
      <Card
        style={{
          minWidth: 360,
          maxWidth: loginBanner ? 560 : 420,
          width: '100%',
        }}
      >
        <CardTitle>Sign in to Skillberry Store</CardTitle>
        <CardBody>
          <Form onSubmit={submit}>
            <Stack hasGutter>
              {loginBanner && (
                <StackItem>
                  <LoginBanner banner={loginBanner} />
                </StackItem>
              )}
              {!loginBanner && loginInfo && (
                <StackItem>
                  {/* Rendered verbatim: no heading, no label, no splitting.
                      PatternFly's Alert requires a `title`, so the whole
                      message goes there as a node — `fontWeight: normal`
                      undoes the heading weight it would otherwise apply, and
                      `pre-line` is what makes the configured line breaks
                      render. React escapes the text, which is the second
                      barrier after the server's html.escape. */}
                  <Alert
                    variant="info"
                    isInline
                    data-testid="login-info"
                    title={
                      <span style={{ whiteSpace: 'pre-line', fontWeight: 'normal' }}>
                        {loginInfo}
                      </span>
                    }
                  />
                </StackItem>
              )}
              {error && (
                <StackItem>
                  <Alert variant="danger" isInline title={error} />
                </StackItem>
              )}
              <StackItem>
                <FormGroup label="Username" isRequired fieldId="login-username">
                  <TextInput
                    isRequired
                    id="login-username"
                    name="username"
                    value={username}
                    onChange={(_e, v) => setUsername(v)}
                    autoComplete="username"
                    autoFocus
                  />
                </FormGroup>
              </StackItem>
              <StackItem>
                <FormGroup label="Password" isRequired fieldId="login-password">
                  <TextInput
                    isRequired
                    type="password"
                    id="login-password"
                    name="password"
                    value={password}
                    onChange={(_e, v) => setPassword(v)}
                    autoComplete="current-password"
                  />
                </FormGroup>
              </StackItem>
              <StackItem>
                <Button
                  type="submit"
                  variant="primary"
                  isBlock
                  isDisabled={submitting || !username || !password}
                >
                  {submitting ? 'Signing in…' : 'Sign in'}
                </Button>
              </StackItem>
            </Stack>
          </Form>
        </CardBody>
        {/* Below CardBody, so it sits beneath the sign-in form AND beneath any
            LoginBanner — an operator's message stays the first thing read.

            Offered here deliberately (docs/design/new_cli.md §5.9): a user who
            cannot sign in yet is exactly the user who wants the CLI, and the
            /cli/* endpoints are unauthenticated by construction, so the modal
            works pre-session with no special case. In `mode: disabled` there is
            no login screen at all and nothing here renders. */}
        <CardFooter>
          {/* Icon-only, matching the masthead's control (AppLayout): the same
              affordance should look the same before and after sign-in, and in
              `mode: disabled` where only the masthead one exists. aria-label,
              not a visible caption, so screen readers and tests can still
              reach it. */}
          <Button
            variant="plain"
            aria-label="Download CLI"
            icon={<DownloadIcon />}
            onClick={() => setIsCliModalOpen(true)}
          />
        </CardFooter>
      </Card>
      <CliDownloadModal
        isOpen={isCliModalOpen}
        onClose={() => setIsCliModalOpen(false)}
      />
    </Bullseye>
  );
}
