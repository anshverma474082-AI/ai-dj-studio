/** The console: one socket, one room, the panels §6.1 asks a participant to see at once.

Everything on screen descends from `useLoungeSocket`, which is the client's only source of room
truth. Nothing here polls for state, and nothing here decides legality — the panels hide what this
role cannot do so the screen is not full of dead buttons, and the server refuses it again on its own
terms if a frame arrives anyway (§10.1).

The playhead is deliberately local (§7.3.4 puts no transport clock on the wire). Chat and reaction
frames stamp it themselves, which is what lets a transcript line point at the passage it was about.
*/
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ApiError,
  chatHistory,
  createLounge,
  currentUser,
  endLounge,
  login as signIn,
  logout,
  readLounge,
  requestSegue,
  type SegueResponse,
  type User,
} from './api/rest';
import { can } from './client/can';
import { useCatalog } from './client/useCatalog';
import { Dashboard } from './panels/Dashboard';
import { DjPanel } from './panels/DjPanel';
import { HostControls } from './panels/HostControls';
import { Chat } from './panels/Chat';
import { LoungeGate, SignIn } from './panels/Entry';
import { NextUp } from './panels/NextUp';
import { NowPlaying } from './panels/NowPlaying';
import { SoundclashStage } from './panels/SoundclashStage';
import { StatusBar } from './panels/StatusBar';
import { useLoungeSocket } from './socket/useLoungeSocket';

/** §10.2's outro window; the server owns the real number and refuses an ask outside it (425). */
const OUTRO_WINDOW_SECONDS = 20;

export function App() {
  const [user, setUser] = useState<User | null>(currentUser());
  const [loungeId, setLoungeId] = useState<string | null>(null);
  const [strip, setStrip] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<SegueResponse | null>(null);
  const [pendingAnswer, setPendingAnswer] = useState(false);
  const catalog = useCatalog();
  const playerRef = useRef<HTMLAudioElement | null>(null);
  const position = useRef(0);
  // The ref is what an ask is measured against, but a ref does not re-render, and the DJ panel's
  // "the window opens in n s" note is read from it. This is the same instant rounded to a second,
  // kept in state so the note moves — and set only when the whole second changes, so a player
  // reporting 60 times a second does not repaint the console 60 times a second.
  const [playhead, setPlayhead] = useState(0);

  const socket = useLoungeSocket(loungeId);
  const { state, send } = socket;

  /** A display name for a userId.

  Only the caller's own is available: the WS snapshot lists seats with roles, chat frames carry
  ids, and there is no directory route. Until #37's dashboard work decides whether one is worth
  exposing, a stranger is shown by role and the head of their id — the same person, the same label,
  on every pane, which is what makes a multi-client screenshot readable.
  */
  const nameOf = useCallback(
    (userId: string | null): string => {
      if (userId === null) return 'the room';
      // The state is `User | null` and this callback is built before the signed-out branch returns,
      // so the null case is a name and not a crash: an unattributed line is still a line.
      const me = user;
      if (me !== null && userId === me.id) return `${me.displayName} (you)`;
      if (userId === state.hostUserId) return `the host · ${userId.slice(0, 6)}`;
      return roleTag(state, userId);
    },
    [state, user],
  );

  // §5.1 persists the transcript; the socket does not replay it. One REST read per room, folded
  // into the same list live frames append to — `loadHistory` dedups by messageId, so a message sent
  // while this fetch was in flight appears once.
  useEffect(() => {
    if (loungeId === null) return;
    let alive = true;
    chatHistory(loungeId)
      .then((page) => {
        if (alive) socket.hydrate(page.messages);
      })
      .catch((reason: unknown) => {
        if (alive) setStrip(reason instanceof ApiError ? reason.message : 'the transcript did not load');
      });
    return () => {
      alive = false;
    };
    // `socket.hydrate` is stable; naming it keeps the effect honest without re-running on every frame.
  }, [loungeId, socket.hydrate]);

  // An answered question is a banter line; a refused one never becomes a frame, so the wait has to
  // end on its own or the panel looks hung on a refusal the reader cannot see.
  useEffect(() => {
    if (!pendingAnswer) return;
    const timer = window.setTimeout(() => setPendingAnswer(false), 5000);
    return () => window.clearTimeout(timer);
  }, [pendingAnswer]);

  useEffect(() => {
    if (state.banter.length > 0 && pendingAnswer) setPendingAnswer(false);
  }, [state.banter.length, pendingAnswer]);

  const frame = useCallback(
    (event: string, payload: Record<string, unknown>) => {
      if (!send(event, payload)) setStrip('the socket is not open yet — that did not reach the room');
    },
    [send],
  );

  const frozen = state.mutationsFrozen;
  const ended = state.status === 'ended';

  const mutationBlock = frozen
    ? 'the queue is frozen: the Host disconnected, and votes resume when their socket returns'
    : ended
      ? 'this session has ended; the queue is history now'
      : state.locked
        ? 'the room is locked — new mutations wait for the Host to unlock it'
        : null;

  const speechBlock =
    mutationBlock ??
    (user !== null && state.mutedUserIds.includes(user.id)
      ? 'you are muted in this lounge; the room still hears the DJ'
      : null);

  const askSegue = async () => {
    if (loungeId === null || state.nowPlaying === null) return;
    setStrip(null);
    try {
      const answer = await requestSegue(loungeId, {
        currentTrackId: state.nowPlaying,
        currentTs: position.current,
        candidateTrackIds: state.nextUp.map((item) => item.trackId),
        currentSentimentScore: state.sentiment.rollingScore,
      });
      setSuggestion(answer);
    } catch (reason) {
      setSuggestion(null);
      setStrip(reason instanceof ApiError ? reason.message : 'the segue request did not answer');
    }
  };

  const enter = async (id: string) => {
    const room = await readLounge(id);
    setLoungeId(room.loungeId);
    setSuggestion(null);
  };

  const create = async (id: string) => {
    setLoungeId(id);
    setSuggestion(null);
  };

  if (user === null) {
    return (
      <SignIn
        onLogin={async (email, password) => {
          const who = await signIn(email, password);
          setUser(who);
        }}
      />
    );
  }

  if (loungeId === null) {
    return (
      <Dashboard
        who={user}
        onEnter={(id) => void enter(id)}
        onSignOut={() => {
          logout();
          setUser(null);
        }}
        gate={
          <LoungeGate
            role={user.role}
            tracks={catalog.tracks}
            onEnter={enter}
            onCreate={async (seedTrackId) => {
              const created = await createLounge({ seedTrackId });
              await create(created.loungeId);
            }}
          />
        }
      />
    );
  }

  return (
    <div className="console">
      <StatusBar
        state={state}
        status={socket.status}
        displayName={user.displayName}
        role={user.role}
        nameOf={nameOf}
        onRequestSnapshot={socket.requestSnapshot}
        onDashboard={() => setLoungeId(null)}
        onSignOut={() => {
          logout();
          setLoungeId(null);
          setUser(null);
        }}
      />

      <div className="banner-stack">
        {state.notice !== null && <p className="banner">{state.notice}</p>}
        {socket.notice !== null && <p className="banner">{socket.notice}</p>}
        {state.lastError !== null && (
          <p className="error-strip">
            the room refused that{state.lastError.status !== null ? ` (${state.lastError.status})` : ''}:{' '}
            {state.lastError.message}
          </p>
        )}
        {strip !== null && <p className="error-strip">{strip}</p>}
      </div>

      <div className="grid">
        <div className="column">
          <NowPlaying
            catalog={catalog}
            nowPlaying={state.nowPlaying}
            live={!ended}
            positionRef={playerRef}
            onPosition={(seconds) => {
              position.current = seconds;
              const whole = Math.floor(seconds);
              setPlayhead((previous) => (previous === whole ? previous : whole));
            }}
          />
          <NextUp
            catalog={catalog}
            queue={state.nextUp}
            canVote={can(user.role, 'vote')}
            canSubmit={can(user.role, 'submitBattle')}
            blockedReason={mutationBlock}
            submissions={state.submissions.map((s) => s.trackId)}
            stageClosed={state.battle !== null}
            onVote={(trackId) => frame('lounge.queue.vote', { trackId })}
            onSubmit={(trackId) => frame('lounge.soundclash.submit', { trackId })}
          />
          <HostControls
            state={state}
            myUserId={user.id}
            myRole={user.role}
            canModerate={can(user.role, 'moderate')}
            nameOf={nameOf}
            onMute={(userId, muted) => frame('lounge.moderate.mute', { userId, muted })}
            onRemove={(userId) => frame('lounge.moderate.remove', { userId })}
            onLock={(locked) => frame('lounge.moderate.lock', { locked })}
            onTransfer={(userId) => frame('lounge.moderate.transfer', { userId })}
            onEnd={() => {
              void endLounge(loungeId)
                .then((closed) => setStrip(`this session ended at ${closed.endedAt}`))
                .catch((reason: unknown) =>
                  setStrip(reason instanceof ApiError ? reason.message : 'the close did not answer'),
                );
            }}
          />
        </div>

        <div className="column">
          <SoundclashStage
            catalog={catalog}
            battle={state.battle}
            lastResult={state.lastResult}
            submissions={state.submissions}
            nameOf={nameOf}
            canOpen={can(user.role, 'openBattle')}
            canVoteBattle={can(user.role, 'voteBattle')}
            canResolve={can(user.role, 'resolveBattle')}
            blockedReason={mutationBlock}
            onSubmitFromCatalog={(trackId) => frame('lounge.soundclash.submit', { trackId })}
            onOpen={(trackIds, durationSeconds) =>
              frame('lounge.soundclash.start', { trackIds, durationSeconds })
            }
            onBallot={(trackId) => frame('lounge.soundclash.vote', { trackId })}
            onForceResolve={() => frame('lounge.soundclash.resolve', {})}
            allTrackIds={catalog.tracks.map((track) => track.id)}
          />
          <DjPanel
            catalog={catalog}
            banter={state.banter}
            suggestion={suggestion}
            canAsk={can(user.role, 'chat')}
            canApprove={can(user.role, 'approveSegue')}
            canAskSegue={can(user.role, 'approveSegue')}
            blockedReason={mutationBlock}
            nowPlaying={state.nowPlaying}
            positionSeconds={playhead}
            durationSeconds={catalog.byId.get(state.nowPlaying ?? '')?.durationSeconds ?? 0}
            outroWindowSeconds={OUTRO_WINDOW_SECONDS}
            candidateTrackIds={state.nextUp.map((item) => item.trackId)}
            pendingAnswer={pendingAnswer}
            onAsk={(question) => {
              setPendingAnswer(true);
              frame('lounge.ai.ask', { question, tsSeconds: position.current });
            }}
            onAskSegue={() => void askSegue()}
            onApprove={(trackId) => frame('lounge.ai.segue.apply', { trackId })}
            onCueOutro={() => {
              const audio = playerRef.current;
              const duration = catalog.byId.get(state.nowPlaying ?? '')?.durationSeconds;
              if (!audio || duration === undefined) return;
              // Forward, to the last twenty seconds of *this record* — the window is measured from
              // the end of the track, not from wherever the playhead happens to be. Landing a
              // second inside the edge keeps the cue valid if the playhead has already drifted in.
              const cue = Math.min(Math.max(0, duration - OUTRO_WINDOW_SECONDS + 1), duration - 1);
              audio.currentTime = cue;
              position.current = cue;
            }}
          />
        </div>

        <div className="column">
          <Chat
            messages={state.messages}
            typers={state.typers}
            myUserId={user.id}
            nameOf={nameOf}
            canChat={can(user.role, 'chat')}
            blockedReason={speechBlock}
            sentiment={state.sentiment}
            positionSeconds={() => position.current}
            onSend={(body, tsSeconds) => frame('lounge.chat.message', { body, tsSeconds })}
            onReact={(emoji, tsSeconds) => frame('lounge.chat.reaction', { emoji, tsSeconds })}
            onTyping={(isTyping) => frame('lounge.chat.typing', { isTyping })}
          />
        </div>
      </div>
    </div>
  );
}

/** A stranger's id, shown as what the room says they are. */
function roleTag(state: { participants: { userId: string; role: string }[] }, userId: string): string {
  const person = state.participants.find((entry) => entry.userId === userId);
  return person === undefined ? `guest ${userId.slice(0, 6)}` : `${person.role} ${userId.slice(0, 6)}`;
}
