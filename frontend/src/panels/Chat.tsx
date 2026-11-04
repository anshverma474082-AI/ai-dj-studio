/** Chat — the room's conversation, and the sentiment signal the DJ curates from.

What lands in this pane is not what was typed. §10.1 makes sanitisation a server job — chat is
stored and re-broadcast to every participant, so an escaped `<script>` here is a stored-XSS payload
with fifty victims per room — and React's own text rendering escapes again on the way out. Two
layers, and neither one is trusted to be the only one.

The composer's own limits mirror the server's (`MAX_MESSAGE_CHARS`) so a too-long message is caught
before it costs a round trip, but the server's refusal is still the answer of record.

Typing is throttled rather than per-keystroke: §6.1 budgets frames per second, and the room only
announces a *change*, so a client that sends one frame per character is making the room do nothing
with them.
*/
import { useEffect, useRef, useState } from 'react';

import { clockPosition } from '../client/format';
import { MAX_MESSAGE_CHARS, REACTION_EMOJIS } from '../client/can';
import type { ChatLine } from '../state/loungeReducer';

const IDLE_TYPING_MS = 3000;

export interface ChatProps {
  messages: ChatLine[];
  typers: string[];
  myUserId: string | null;
  nameOf: (userId: string | null) => string;
  canChat: boolean;
  blockedReason: string | null;
  sentiment: { rollingScore: number; windowSeconds: number; sampleSize: number };
  /** This client's own playhead: §7.3.4's `tsSeconds` stamps a line at the moment it was spoken. */
  positionSeconds: () => number;
  onSend: (body: string, tsSeconds: number) => void;
  onReact: (emoji: string, tsSeconds: number) => void;
  onTyping: (isTyping: boolean) => void;
}

export function Chat({
  messages,
  typers,
  myUserId,
  nameOf,
  canChat,
  blockedReason,
  sentiment,
  positionSeconds,
  onSend,
  onReact,
  onTyping,
}: ChatProps) {
  const [draft, setDraft] = useState('');
  const feed = useRef<HTMLDivElement | null>(null);
  const idleTimer = useRef<number | null>(null);
  const typingNow = useRef(false);

  useEffect(() => {
    const node = feed.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages.length]);

  useEffect(() => {
    return () => {
      if (idleTimer.current !== null) window.clearTimeout(idleTimer.current);
    };
  }, []);

  const markTyping = (value: boolean) => {
    if (value === typingNow.current) return;
    typingNow.current = value;
    onTyping(value);
  };

  const scheduleIdle = () => {
    if (idleTimer.current !== null) window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => markTyping(false), IDLE_TYPING_MS);
  };

  const send = () => {
    const body = draft.trim();
    if (body === '' || blockedReason !== null || !canChat) return;
    onSend(body.slice(0, MAX_MESSAGE_CHARS), positionSeconds());
    setDraft('');
    markTyping(false);
  };

  const remaining = MAX_MESSAGE_CHARS - draft.length;
  const others = typers.filter((id) => id !== myUserId);

  return (
    <section className="card chat" aria-label="Chat">
      <h2>
        Chat <Mood sentiment={sentiment} />
      </h2>

      <div className="feed" ref={feed}>
        {messages.map((line) => (
          <p key={line.id} className={`line ${line.kind} ${line.sentiment ?? ''}`}>
            {line.kind === 'chat' && (
              <>
                <strong>{nameOf(line.userId)}</strong> <span>{line.body}</span>{' '}
                <em className="bucket">{line.sentiment ?? 'neutral'}</em>
              </>
            )}
            {line.kind === 'reaction' && (
              <>
                <strong>{nameOf(line.userId)}</strong> <span className="emoji">{line.body}</span>
              </>
            )}
            {line.kind === 'answer' && <span className="dj">AI DJ · {line.body}</span>}
            {line.kind === 'announcement' && <span className="dj">AI DJ · {line.body}</span>}
            {line.kind === 'system' && <span>{line.body}</span>}
            <time>{clockPosition(line.tsSeconds)}</time>
          </p>
        ))}
        {messages.length === 0 && <p className="empty">the room has not said anything yet</p>}
      </div>

      <p className="typers" aria-live="polite">
        {others.length > 0 ? `${others.map(nameOf).join(', ')} is typing…` : ''}
      </p>

      {blockedReason !== null ? (
        <p className="frozen-note">{blockedReason}</p>
      ) : (
        <>
          <div className="composer">
            <input
              value={draft}
              maxLength={MAX_MESSAGE_CHARS}
              placeholder={canChat ? 'say something about the record' : 'this role may not speak here'}
              disabled={!canChat}
              onChange={(event) => {
                setDraft(event.currentTarget.value);
                markTyping(true);
                scheduleIdle();
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  send();
                }
              }}
            />
            <button type="button" onClick={send} disabled={!canChat || draft.trim() === ''}>
              Send
            </button>
          </div>
          <p className="counter">
            {remaining} characters left · the server sanitises and scores every line
          </p>
          <div className="reactions">
            {REACTION_EMOJIS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                className="emoji-button"
                onClick={() => onReact(emoji, positionSeconds())}
                disabled={!canChat}
                title={`react ${emoji}`}
              >
                {emoji}
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

/** §6.2's rolling mood. `sampleSize` is shown because a reading from two messages is a weaker
 *  claim than one from forty, and a meter that cannot admit its own thinness invites over-trust. */
function Mood({ sentiment }: { sentiment: ChatProps['sentiment'] }) {
  const pct = Math.round(sentiment.rollingScore * 100);
  return (
    <span className="mood" title={`${sentiment.sampleSize} samples in a ${sentiment.windowSeconds}s window`}>
      <span className="meter">
        <span className="fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="score">{pct}%</span>
      <span className="samples">{sentiment.sampleSize}</span>
    </span>
  );
}
