/** Getting into a room: sign in, then name the lounge.

The demo accounts are prefilled on the form on purpose. They are seeded credentials from
`scripts/seed.py`, listed in the README, and worth nothing outside this database — and a reviewer
who can be at a keyboard in ten seconds is worth more than a login box that looks tidier.

A listener is handed an invite code, never a lounge id. §5.1's journey says "join a Lounge via
invite code", and `GET /by-invite/{code}` is the route that makes that true; the raw id box exists for
whoever is holding one from the dashboard or a create response.
*/
import { useState } from 'react';

import { ApiError, lookupInvite, type Role, type Track } from '../api/rest';
import { can } from '../client/can';

const DEMO_PASSWORD = 'djstudio-demo';
const DEMO_ACCOUNTS: { email: string; label: string }[] = [
  { email: 'host@example.com', label: 'Riya (Host)' },
  { email: 'codj@example.com', label: 'Kabir (Co-DJ)' },
  { email: 'listener@example.com', label: 'Meera (Listener)' },
  { email: 'admin@example.com', label: 'Studio Admin' },
];

export interface SignInProps {
  onLogin: (email: string, password: string) => Promise<void>;
}

export function SignIn({ onLogin }: SignInProps) {
  const [email, setEmail] = useState('host@example.com');
  const [password, setPassword] = useState(DEMO_PASSWORD);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await onLogin(email.trim(), password);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : 'the sign-in did not answer');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="entry">
      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <h1>AI DJ Studio</h1>
        <p className="sub">collaborative live queue curation, with a sentiment-aware DJ</p>

        <label>
          email
          <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" />
        </label>
        <label>
          password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>

        <div className="row">
          {DEMO_ACCOUNTS.map((account) => (
            <button
              key={account.email}
              type="button"
              className="ghost"
              onClick={() => {
                setEmail(account.email);
                setPassword(DEMO_PASSWORD);
              }}
            >
              {account.label}
            </button>
          ))}
        </div>

        <button type="submit" disabled={busy || email.trim() === '' || password === ''}>
          {busy ? 'signing in…' : 'sign in'}
        </button>
        {error !== null && <p className="error-strip">{error}</p>}
      </form>
    </main>
  );
}

export interface LoungeGateProps {
  role: Role;
  tracks: Track[];
  /** Resolve an invite code to the lounge id every other route is keyed by, then seat the caller. */
  onEnter: (loungeId: string) => Promise<void>;
  onCreate: (seedTrackId: string) => Promise<void>;
}

export function LoungeGate({ role, tracks, onEnter, onCreate }: LoungeGateProps) {
  const [mode, setMode] = useState<'invite' | 'id' | 'create'>('invite');
  const [code, setCode] = useState('SP-DEMO');
  const [id, setId] = useState('');
  const [seed, setSeed] = useState(tracks[0]?.id ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = (action: () => Promise<void>) => async () => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : 'the room did not answer');
    } finally {
      setBusy(false);
    }
  };

  const byInvite = run(async () => {
    const found = await lookupInvite(code);
    if (found.status === 'ended') {
      throw new ApiError(410, `the lounge behind ${found.loungeId.slice(0, 8)} has ended`);
    }
    await onEnter(found.loungeId);
  });

  return (
    <section className="card gate" aria-label="Enter a lounge">
      <h2>Enter a lounge</h2>
        <p className="sub">signed in as {role}</p>

        <div className="row tabs">
          <button type="button" className={mode === 'invite' ? 'on' : 'ghost'} onClick={() => setMode('invite')}>
            invite code
          </button>
          <button type="button" className={mode === 'id' ? 'on' : 'ghost'} onClick={() => setMode('id')}>
            lounge id
          </button>
          {can(role, 'createLounge') && (
            <button type="button" className={mode === 'create' ? 'on' : 'ghost'} onClick={() => setMode('create')}>
              open a new one
            </button>
          )}
        </div>

        {mode === 'invite' && (
          <>
            <label>
              invite code
              <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="SP-DEMO" />
            </label>
            <button type="button" onClick={byInvite} disabled={busy || code.trim() === ''}>
              {busy ? 'joining…' : 'join'}
            </button>
          </>
        )}

        {mode === 'id' && (
          <>
            <label>
              lounge id
              <input value={id} onChange={(event) => setId(event.target.value)} placeholder="32-character id" />
            </label>
            <button type="button" onClick={run(() => onEnter(id.trim()))} disabled={busy || id.trim() === ''}>
              {busy ? 'joining…' : 'join'}
            </button>
          </>
        )}

        {mode === 'create' && (
          <>
            <label>
              seed track
              <select value={seed} onChange={(event) => setSeed(event.target.value)}>
                {tracks.map((track) => (
                  <option key={track.id} value={track.id}>
                    {track.title} — {track.artist} · {track.bpm} BPM
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={run(() => onCreate(seed))} disabled={busy || seed === ''}>
              {busy ? 'opening…' : 'open this lounge'}
            </button>
          </>
        )}

        {error !== null && <p className="error-strip">{error}</p>}
    </section>
  );
}
