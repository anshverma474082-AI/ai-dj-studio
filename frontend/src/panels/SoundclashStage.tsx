/** Soundclash Stage — the crowd's challenge, §6.2.

Three rooms in one pane, because the stage is one object with three states:

* **Nothing open** — the submission pool, waiting for a Host to put it to a vote. Two-to-four
  records is the room's own bound; the Host may narrow the pool but never add to it, which is why
  the checkboxes start checked.
* **A battle running** — the tally and the countdown. The count arrives as a whole list on every
  frame, so a client that missed a ballot still shows what the room holds rather than its own
  arithmetic.
* **Just resolved** — the last result, until the next open or a resync clears it. The winner is
  already in the queue by the time this pane shows the line, so the result is a caption on a fact
  the reader can check next door.

Timers are wall clock: `closesAt` is epoch milliseconds (§7.3.4), published so a client that joined
mid-battle can count down from the server's deadline rather than from when its own socket opened.
*/
import { useEffect, useState } from 'react';

import { countdown } from '../client/format';
import type { Catalog } from '../client/useCatalog';
import type { Battle, LoungeState } from '../state/loungeReducer';

export interface StageProps {
  catalog: Catalog;
  battle: Battle | null;
  lastResult: LoungeState['lastResult'];
  submissions: { trackId: string; submittedByUserId: string }[];
  nameOf: (userId: string | null) => string;
  canOpen: boolean;
  canVoteBattle: boolean;
  canResolve: boolean;
  /** Votes are frozen while the Host is gone; the stage is a mutation like any other. */
  blockedReason: string | null;
  onSubmitFromCatalog: (trackId: string) => void;
  onOpen: (trackIds: string[], durationSeconds: number) => void;
  onBallot: (trackId: string) => void;
  onForceResolve: () => void;
  /** The catalog's whole list, so a listener can challenge something the queue never held. */
  allTrackIds: string[];
}

const DEFAULT_VOTE_SECONDS = 30;
const MIN_CANDIDATES = 2;
const MAX_CANDIDATES = 4;

export function SoundclashStage({
  catalog,
  battle,
  lastResult,
  submissions,
  nameOf,
  canOpen,
  canVoteBattle,
  canResolve,
  blockedReason,
  onSubmitFromCatalog,
  onOpen,
  onBallot,
  onForceResolve,
  allTrackIds,
}: StageProps) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [seconds, setSeconds] = useState(DEFAULT_VOTE_SECONDS);

  useEffect(() => {
    setPicked(new Set(battle === null ? submissions.map((s) => s.trackId) : []));
  }, [battle, submissions.length]);

  // A countdown needs a now that moves; without this the number would only refresh when the room
  // sent something, and a silent battle would look frozen rather than finished.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (battle === null) return;
    const tick = () => setNow(Date.now());
    // Seeded on the way in, not only on the interval: `now` is refreshed only while a battle runs,
    // so at the instant one opens it still holds whenever this panel first mounted. Skipping the
    // seed showed 824s left on an 8-second vote for its first whole second.
    tick();
    const timer = window.setInterval(tick, 1000);
    // A background tab throttles intervals to roughly one per minute, and a Host who checks
    // another window mid-vote would come back to a countdown that never moved.
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [battle]);

  const chosen = submissions.filter((s) => picked.has(s.trackId));
  const queueOnly = allTrackIds.filter((id) => !submissions.some((s) => s.trackId === id));

  return (
    <section className="card" aria-label="Soundclash stage">
      <h2>Soundclash stage</h2>
      {blockedReason !== null && <p className="frozen-note">{blockedReason}</p>}

      {battle === null ? (
        <>
          {submissions.length === 0 ? (
            <p className="empty">nobody has put a record forward yet</p>
          ) : (
            <ul className="battle-candidates">
              {submissions.map((submission) => {
                const track = catalog.byId.get(submission.trackId);
                return (
                  <li key={submission.trackId}>
                    <label>
                      <input
                        type="checkbox"
                        checked={picked.has(submission.trackId)}
                        onChange={() =>
                          setPicked((previous) => {
                            const next = new Set(previous);
                            if (next.has(submission.trackId)) next.delete(submission.trackId);
                            else next.add(submission.trackId);
                            return next;
                          })
                        }
                      />
                      <strong>{track?.title ?? submission.trackId}</strong>
                      <em>
                        {track ? `${track.bpm} BPM · ${track.camelotKey}` : 'not in the catalog'} ·
                        asked by {nameOf(submission.submittedByUserId)}
                      </em>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="stage-controls">
            <label className="duration">
              vote for{' '}
              <input
                type="number"
                min={5}
                max={120}
                step={5}
                value={seconds}
                onChange={(event) => setSeconds(Number(event.currentTarget.value))}
              />{' '}
              seconds
            </label>
            <button
              type="button"
              onClick={() => onOpen(chosen.map((s) => s.trackId), seconds)}
              disabled={
                blockedReason !== null ||
                !canOpen ||
                chosen.length < MIN_CANDIDATES ||
                chosen.length > MAX_CANDIDATES
              }
              title={
                chosen.length < MIN_CANDIDATES
                  ? `a battle needs ${MIN_CANDIDATES}-${MAX_CANDIDATES} candidates`
                  : blockedReason ?? 'put these to a vote'
              }
            >
              Open battle ({chosen.length})
            </button>
          </div>

          {canVoteBattle && blockedReason === null && queueOnly.length > 0 && (
            <details className="challenge">
              <summary>Challenge with something not in the queue</summary>
              <ul>
                {queueOnly.map((trackId) => {
                  const track = catalog.byId.get(trackId);
                  return (
                    <li key={trackId}>
                      <button type="button" onClick={() => onSubmitFromCatalog(trackId)}>
                        {track?.title ?? trackId}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </details>
          )}

          {lastResult !== null && (
            <p className="result">
              {lastResult.winningTrackId === null
                ? 'the last battle ended in a stalemate — nothing was promoted'
                : `“${catalog.byId.get(lastResult.winningTrackId)?.title ?? lastResult.winningTrackId}” won the last battle`}
            </p>
          )}
        </>
      ) : (
        <>
          <p className="deadline">
            {countdown(battle.closesAt, now)} · {battle.candidateTrackIds.length} challengers
          </p>
          <ul className="tally">
            {battle.candidateTrackIds.map((trackId, index) => {
              const track = catalog.byId.get(trackId);
              const votes = battle.tally[index] ?? 0;
              const total = battle.tally.reduce((a, b) => a + b, 0);
              return (
                <li key={trackId} className="tally-row">
                  <strong>{track?.title ?? trackId}</strong>
                  <span className="meter">
                    <span
                      className="fill"
                      style={{ width: total === 0 ? '0%' : `${(votes / total) * 100}%` }}
                    />
                  </span>
                  <span className="count">{votes}</span>
                  <button
                    type="button"
                    onClick={() => onBallot(trackId)}
                    disabled={blockedReason !== null || !canVoteBattle}
                  >
                    Ballot
                  </button>
                </li>
              );
            })}
          </ul>
          {canResolve && (
            <button type="button" className="force" onClick={onForceResolve}>
              Close the vote now
            </button>
          )}
        </>
      )}
    </section>
  );
}
