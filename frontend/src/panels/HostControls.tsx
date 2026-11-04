/** Host controls — §6.1's moderation panel, and the room's end.

The roster is what the last snapshot reported. A presence frame names a person and moves the count
but carries no role, so someone who sat down after this client's snapshot appears in the count and
not in this list; the console says so rather than guessing a role, and "resync" refreshes the list.
That gap exists because a role is a fact about the room, not about a frame — the server is the one
that seated them and knows it.

Muting is not deleting. §6.1's rule is that a muted participant keeps their seat and their socket and
loses speech, which is why the control reads as a toggle and why the row stays in the list.

The owner is `snapshot.hostUserId`, not the caller's role: §7.2.1 keeps that column per lounge
because a global "host" role does not govern a single room, and a transferred Host gains the room
without gaining the role.
*/
import { canEndSession } from '../client/can';
import type { Role } from '../api/rest';
import type { LoungeState } from '../state/loungeReducer';

export interface HostControlsProps {
  state: LoungeState;
  myUserId: string | null;
  myRole: Role | null;
  canModerate: boolean;
  nameOf: (userId: string | null) => string;
  onMute: (userId: string, muted: boolean) => void;
  onRemove: (userId: string) => void;
  onLock: (locked: boolean) => void;
  onTransfer: (userId: string) => void;
  onEnd: () => void;
}

export function HostControls({
  state,
  myUserId,
  myRole,
  canModerate,
  nameOf,
  onMute,
  onRemove,
  onLock,
  onTransfer,
  onEnd,
}: HostControlsProps) {
  const muted = new Set(state.mutedUserIds);
  const ended = state.status === 'ended';
  return (
    <section className="card" aria-label="Host controls">
      <h2>Host controls</h2>
      {!canModerate && (
        <p className="frozen-note">
          this role may not moderate; the room decides, not the page
        </p>
      )}

      <table className="roster">
        <thead>
          <tr>
            <th>seated</th>
            <th>role</th>
            <th> </th>
          </tr>
        </thead>
        <tbody>
          {state.participants.map((person) => {
            const isOwner = person.userId === state.hostUserId;
            const isMe = person.userId === myUserId;
            return (
              <tr key={person.userId} className={isOwner ? 'owner' : ''}>
                <td>
                  {nameOf(person.userId)}
                  {isOwner && <em className="tag">host</em>}
                  {isMe && <em className="tag">you</em>}
                  {muted.has(person.userId) && <em className="tag muted">muted</em>}
                </td>
                <td>{person.role}</td>
                <td className="row-actions">
                  <button
                    type="button"
                    disabled={!canModerate || isOwner || ended}
                    onClick={() => onMute(person.userId, !muted.has(person.userId))}
                    title={isOwner ? 'the host is not muteable' : undefined}
                  >
                    {muted.has(person.userId) ? 'unmute' : 'mute'}
                  </button>
                  <button
                    type="button"
                    className="danger"
                    disabled={!canModerate || isOwner || isMe || ended}
                    onClick={() => onRemove(person.userId)}
                  >
                    remove
                  </button>
                  <button
                    type="button"
                    disabled={!canModerate || isOwner || isMe || ended}
                    onClick={() => onTransfer(person.userId)}
                    title="they must already be seated"
                  >
                    make host
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {state.participantCount > state.participants.length && (
        <p className="counter">
          {state.participantCount} seated, {state.participants.length} named by the last snapshot —
          resync to refresh the roster
        </p>
      )}

      <div className="row">
        <button
          type="button"
          disabled={!canModerate || ended}
          onClick={() => onLock(!state.locked)}
        >
          {state.locked ? 'unlock the room' : 'lock the room'}
        </button>
        {canEndSession(myRole, myUserId, state.hostUserId) && !ended && (
          <button type="button" className="danger" onClick={onEnd}>
            end this session
          </button>
        )}
      </div>
      {ended && (
        <p className="frozen-note">
          this lounge ended at {state.endedAt ?? 'an unstated time'} — its history is still readable
        </p>
      )}
    </section>
  );
}
