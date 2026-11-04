/** The AI DJ — what it said, what it would play next, and what you can ask it.

Three things the PRD keeps separate and a console must not blur:

* **Banter** (§6.2) is grounded. Every line cites the records it came from, and a decline is shown
  as a decline — the DJ does not invent a segue that breaks the room's own rules, and it does not
  invent a fact about a track whose timeline is empty.
* **The suggestion** is a read-only question to the matcher (`POST /ai/segue`). Applying it is a
  separate act the Host approves, so a page that polls the outro window cannot move the room by
  itself.
* **The ask** (§7.1) travels on its own socket event rather than as chat, because a question must
  not sample the room's mood and its answer must not look like it came from a person.

"cue the outro" exists for the demo: §7.3.3 refuses a segue asked before the window opens (425),
and a reviewer is not going to wait three and a half minutes for a record to reach its last twenty
seconds. It seeks this client's own player — exactly what a scrub bar already does — and touches no
room state.
*/
import { useState } from 'react';

import type { SegueResponse } from '../api/rest';
import { clockTotal } from '../client/format';
import type { BanterLine } from '../state/loungeReducer';
import type { Catalog } from '../client/useCatalog';

export interface DjPanelProps {
  catalog: Catalog;
  banter: BanterLine[];
  suggestion: SegueResponse | null;
  canAsk: boolean;
  canApprove: boolean;
  canAskSegue: boolean;
  /** Frozen queue, locked room or ended session — approving a segue is a mutation. */
  blockedReason: string | null;
  nowPlaying: string | null;
  positionSeconds: number;
  durationSeconds: number;
  outroWindowSeconds: number;
  candidateTrackIds: string[];
  pendingAnswer: boolean;
  onAsk: (question: string) => void;
  onAskSegue: () => void;
  onApprove: (trackId: string) => void;
  onCueOutro: () => void;
}

export function DjPanel({
  catalog,
  banter,
  suggestion,
  canAsk,
  canApprove,
  canAskSegue,
  blockedReason,
  nowPlaying,
  positionSeconds,
  durationSeconds,
  outroWindowSeconds,
  candidateTrackIds,
  pendingAnswer,
  onAsk,
  onAskSegue,
  onApprove,
  onCueOutro,
}: DjPanelProps) {
  const [question, setQuestion] = useState('');

  const outroOpensAt = Math.max(0, durationSeconds - outroWindowSeconds);
  const inWindow = positionSeconds >= outroOpensAt;
  const selected = suggestion?.selectedTrackId ?? null;

  const ask = () => {
    const text = question.trim();
    if (text === '' || !canAsk) return;
    onAsk(text);
    setQuestion('');
  };

  return (
    <section className="card" aria-label="AI DJ">
      <h2>AI DJ</h2>

      <div className="banter">
        {banter.length === 0 ? (
          <p className="empty">the DJ has not spoken yet</p>
        ) : (
          banter.map((line) => (
            <p key={line.id} className={`banter-line ${line.declined ? 'declined' : ''}`}>
              <span>{line.text}</span>
              {line.question !== null && <em className="asked">asked: “{line.question}”</em>}
              <em className="cites">
                {line.relatedTrackId !== null &&
                  ` about ${catalog.byId.get(line.relatedTrackId)?.title ?? line.relatedTrackId}`}
                {line.sourceEvents.length > 0 &&
                  ` · cited ${line.sourceEvents.length} timeline event${line.sourceEvents.length === 1 ? '' : 's'}`}
                {line.declined && ' · declined'}
              </em>
            </p>
          ))
        )}
      </div>

      <div className="segue">
        <h3>Next-record suggestion</h3>
        <p className="window-note">
          {nowPlaying === null
            ? 'nothing is playing to segue from'
            : `outro window opens at ${clockTotal(outroOpensAt)} · your playhead is at ${clockTotal(positionSeconds)}`}
        </p>
        <div className="row">
          <button
            type="button"
            onClick={onAskSegue}
            disabled={
              !canAskSegue || nowPlaying === null || candidateTrackIds.length < 1 || blockedReason !== null
            }
            title={
              candidateTrackIds.length === 0
                ? 'the queue holds nothing to choose from'
                : 'ask the matcher what fits next'
            }
          >
            Ask the DJ
          </button>
          {nowPlaying !== null && !inWindow && (
            <button type="button" onClick={onCueOutro} className="ghost">
              cue the outro
            </button>
          )}
        </div>

        {suggestion !== null && (
          <div className="suggestion">
            {selected === null ? (
              <p className="declined">{suggestion.declineReason ?? 'the DJ declined'}</p>
            ) : (
              <>
                <p>
                  <strong>{catalog.byId.get(selected)?.title ?? selected}</strong>
                  {suggestion.matchReason !== null && (
                    <em>
                      {' '}
                      {suggestion.matchReason.bpmDeltaPct >= 0 ? '+' : ''}
                      {suggestion.matchReason.bpmDeltaPct.toFixed(1)}% BPM ·{' '}
                      {suggestion.matchReason.camelotRelation} · energy fit{' '}
                      {Math.round(suggestion.matchReason.energyFit * 100)}%
                    </em>
                  )}
                </p>
                <p className="script">{suggestion.transitionScript}</p>
                <p className="cites">
                  grounded on {suggestion.sourceEvents.length} authored timeline event
                  {suggestion.sourceEvents.length === 1 ? '' : 's'}
                </p>
                {blockedReason === null && canApprove && (
                  <button type="button" onClick={() => onApprove(selected)}>
                    Play it next
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </div>

      <div className="composer">
        <input
          value={question}
          onChange={(event) => setQuestion(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              ask();
            }
          }}
          placeholder="ask the DJ about this record"
          maxLength={500}
          disabled={!canAsk}
        />
        <button type="button" onClick={ask} disabled={!canAsk || question.trim() === ''}>
          {pendingAnswer ? 'asking…' : 'Ask'}
        </button>
      </div>
      <p className="counter">
        answered from authored metadata only — the DJ declines rather than invents
      </p>
    </section>
  );
}
