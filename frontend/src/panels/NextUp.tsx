/** Next Up — the queue the room is arguing about, §5.1's second screen.

The order on this list is not this client's arrangement of a local array. §6.1 requires it to be
identical on every screen, which is why `lounge.queue.update` carries the *whole* queue and why the
reducer takes it verbatim; the pane draws what it was handed, in the order it was handed it.

Two controls, and both are acts on the room rather than on the page: a vote moves a record toward
the front, and "to stage" opens a challenge for a slot the queue never offered.
*/
import { clockTotal } from '../client/format';
import type { Catalog } from '../client/useCatalog';
import type { QueueItem } from '../state/loungeReducer';

export interface NextUpProps {
  catalog: Catalog;
  queue: QueueItem[];
  canVote: boolean;
  canSubmit: boolean;
  /** The Host's socket dropped and the queue froze, the room is locked, or the session ended. All
   *  three stop votes; the caller says which one in the words the room would use. */
  blockedReason: string | null;
  /** Track ids already on the stage — a submission is one challenge per record. */
  submissions: string[];
  /** A battle running means the stage itself is closed to new entries (§6.2). */
  stageClosed: boolean;
  onVote: (trackId: string) => void;
  onSubmit: (trackId: string) => void;
}

export function NextUp({
  catalog,
  queue,
  canVote,
  canSubmit,
  blockedReason,
  submissions,
  stageClosed,
  onVote,
  onSubmit,
}: NextUpProps) {
  const onStage = new Set(submissions);
  return (
    <section className="card" aria-label="Next up">
      <h2>Next up</h2>
      {blockedReason !== null && <p className="frozen-note">{blockedReason}</p>}
      {queue.length === 0 ? (
        <p className="empty">{catalog.loading ? 'loading the catalog…' : 'the queue is empty'}</p>
      ) : (
        <ol className="queue">
          {queue.map((item, index) => {
            const track = catalog.byId.get(item.trackId);
            const cannotVote = blockedReason !== null || !canVote;
            return (
              <li key={item.trackId} className="queue-row">
                {/* The reader counts from one. `item.position` is the room's sparse key — 1000
                    apart so a vote can move a record without renumbering the queue (§9.4) — and
                    showing it would put "2000" next to the second record for no reason a person
                    voted for. It stays on the element for anyone checking the order. */}
                <span className="pos" title={`queue position ${item.position}`}>
                  {index + 1}
                </span>
                <span className="who">
                  <strong>{track?.title ?? item.trackId}</strong>
                  <em>
                    {track
                      ? `${track.artist} · ${track.bpm} BPM · ${track.camelotKey} · ${clockTotal(track.durationSeconds)}`
                      : 'not in the catalog'}
                  </em>
                </span>
                <span className="votes">
                  {item.voteCount} vote{item.voteCount === 1 ? '' : 's'}
                </span>
                <span className="row-actions">
                  <button
                    type="button"
                    onClick={() => onVote(item.trackId)}
                    disabled={cannotVote}
                    title={cannotVote ? (blockedReason ?? 'this role may not vote') : 'move this forward'}
                  >
                    Vote
                  </button>
                  <button
                    type="button"
                    className="stage"
                    onClick={() => onSubmit(item.trackId)}
                    disabled={cannotVote || !canSubmit || stageClosed || onStage.has(item.trackId)}
                    title={
                      stageClosed
                        ? 'a battle is running; the stage reopens when it resolves'
                        : onStage.has(item.trackId)
                          ? 'already on the stage'
                          : 'challenge for the next slot'
                    }
                  >
                    To stage
                  </button>
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
