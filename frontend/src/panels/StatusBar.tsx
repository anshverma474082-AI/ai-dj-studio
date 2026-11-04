/** The room's status line: who is in it, whether this client's view is current, and what the
 *  server thinks the time is.

`serverDriftMs` is not a flourish. §1.3 states a <100 ms sync target and §7.3.4's `lounge.sync.ping`
exists to measure it, so the console draws the number it measures rather than claiming the target is
met. A negative value is what a real clock skew looks like, and it is shown rather than clamped —
hiding the sign would hide the finding.
*/
import type { SocketStatus } from '../socket/useLoungeSocket';
import type { LoungeState } from '../state/loungeReducer';

export interface StatusBarProps {
  state: LoungeState;
  status: SocketStatus;
  displayName: string | null;
  role: string | null;
  nameOf: (userId: string | null) => string;
  onRequestSnapshot: () => void;
  onSignOut: () => void;
}

export function StatusBar({
  state,
  status,
  displayName,
  role,
  nameOf,
  onRequestSnapshot,
  onSignOut,
}: StatusBarProps) {
  const drift = state.serverDriftMs;
  return (
    <header className="status-bar">
      <span className={`dot ${status}`} aria-label={`socket ${status}`} />
      <strong className="lounge">{state.loungeId === null ? 'AI DJ Studio' : 'lounge ' + short(state.loungeId)}</strong>
      <span className="meta">
        {state.participantCount} seated · revision {state.revision} ·{' '}
        {state.status === 'unknown' ? 'status pending' : state.status}
      </span>
      <span className="meta" title="clock difference measured by lounge.sync.ping">
        {drift === null ? 'drift —' : `drift ${drift}ms`}
      </span>
      <span className="who">
        {displayName ?? 'signed out'}
        {role !== null && <em> {role}</em>}
        {state.hostUserId !== null && ` · ${nameOf(state.hostUserId)}`}
      </span>
      <button type="button" className="ghost" onClick={onRequestSnapshot} title="refetch the room's whole state">
        resync
      </button>
      <button type="button" className="ghost" onClick={onSignOut}>
        sign out
      </button>
    </header>
  );
}

function short(id: string): string {
  return id.slice(0, 8);
}
