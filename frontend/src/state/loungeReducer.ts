/** The console's whole model of the room, built from the frames §7.3.4 puts on the wire.
 *
 * Pure on purpose: `applyFrame(state, frame) -> {state, resync}` is the only place the client
 * decides what a screen means, and it can be tested without a browser, a DOM, or a server. The
 * hook that owns the socket does one thing with the answer — render it, and ask for a snapshot
 * when `resync` is true.
 *
 * Two rules worth stating because they are the ones a client gets wrong:
 *
 * **The server publishes whole lists, so the client takes them.** `nextUp`, the battle tally, the
 * submission pool and the typers list all arrive complete. A client that merged deltas would end up
 * with an order the room never had — the exact disagreement §6.1 forbids — and would be the reason
 * two screens show a different count for the same ballot.
 *
 * **A revision is a promise about order, not a number to display.** An update at or below what this
 * client holds is dropped; an update that skips one is newer than what we have *and* proof that
 * something was missed, so it is applied and a snapshot is requested. The snapshot is the only
 * thing that can fill the hole, because the hole is not in the queue — it is in every frame that
 * arrived while the socket was down.
 */

export interface Frame {
  event: string;
  loungeId: string;
  payload: unknown;
  ts: number;
}

export interface QueueItem {
  trackId: string;
  position: number;
  voteCount: number;
}

export interface Battle {
  battleId: string;
  candidateTrackIds: string[];
  /** Epoch milliseconds, the unit `open_frame` publishes in. */
  closesAt: number;
  /** One number per candidate, in `candidateTrackIds` order. */
  tally: number[];
}

export interface ChatLine {
  id: string;
  kind: 'chat' | 'reaction' | 'answer' | 'announcement' | 'system';
  userId: string | null;
  body: string;
  sentiment: string | null;
  tsSeconds: number | null;
}

export interface BanterLine {
  id: string;
  kind: 'announcement' | 'answer';
  text: string;
  question: string | null;
  relatedTrackId: string | null;
  sourceEvents: string[];
  declined: boolean;
}

export interface LoungeState {
  loungeId: string | null;
  /** The lounge row's lifecycle: announced by `lounge.status.update`, and handed to a joiner by
   *  its snapshot, because a tab opened after the room went live never sees that frame. */
  status: string;
  revision: number;
  hostUserId: string | null;
  nowPlaying: string | null;
  nextUp: QueueItem[];
  battle: Battle | null;
  lastResult: { battleId: string; winningTrackId: string | null } | null;
  submissions: { trackId: string; submittedByUserId: string }[];
  sentiment: { rollingScore: number; windowSeconds: number; sampleSize: number };
  participantCount: number;
  /** As of the last snapshot. Presence frames name a person but carry no role, so a join after
   *  this frame moves the count and the feed, and the roster is refreshed by asking again. */
  participants: { userId: string; role: string }[];
  presenceFeed: { participantId: string; action: string }[];
  typers: string[];
  messages: ChatLine[];
  banter: BanterLine[];
  locked: boolean;
  mutedUserIds: string[];
  hostDisconnected: boolean;
  mutationsFrozen: boolean;
  endedAt: string | null;
  lastError: { message: string; status: number | null } | null;
  notice: string | null;
  serverDriftMs: number | null;
}

export const MESSAGE_WINDOW = 200;
export const BANTER_WINDOW = 50;
export const PRESENCE_WINDOW = 20;

export const initialState: LoungeState = {
  loungeId: null,
  status: 'unknown',
  revision: 0,
  hostUserId: null,
  nowPlaying: null,
  nextUp: [],
  battle: null,
  lastResult: null,
  submissions: [],
  sentiment: { rollingScore: 0.5, windowSeconds: 45, sampleSize: 0 },
  participantCount: 0,
  participants: [],
  presenceFeed: [],
  typers: [],
  messages: [],
  banter: [],
  locked: false,
  mutedUserIds: [],
  hostDisconnected: false,
  mutationsFrozen: false,
  endedAt: null,
  lastError: null,
  notice: null,
  serverDriftMs: null,
};

export interface Reduced {
  state: LoungeState;
  /** True when this client cannot account for the frames it missed, so a snapshot is the only fix. */
  resync: boolean;
}

type Body = Record<string, any>;

function body(frame: Frame): Body {
  return (frame.payload ?? {}) as Body;
}

function text(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function applyFrame(state: LoungeState, frame: Frame): Reduced {
  const payload = body(frame);
  // A refusal is worth one strip on the screen, and nothing more: the state it refused on is
  // still the room's. Every other frame the room sends replaces it.
  const base: LoungeState = frame.event === 'lounge.error' ? state : { ...state, lastError: null };

  switch (frame.event) {
    case 'lounge.snapshot':
      return {
        state: {
          ...base,
          loungeId: text(payload.loungeId) ?? base.loungeId,
          revision: num(payload.revision) ?? base.revision,
          status: text(payload.status) ?? base.status,
          hostUserId: text(payload.hostUserId),
          nowPlaying: text(payload.nowPlaying),
          nextUp: (payload.nextUp ?? []) as QueueItem[],
          battle: battleFrom(payload.soundclash),
          // A resync is the room answering "what is true now", and a last result is not part of
          // that answer. Keeping it would let a stale winner sit on the stage after a reload.
          lastResult: null,
          submissions: (payload.soundclashSubmissions ?? []) as LoungeState['submissions'],
          sentiment: {
            rollingScore: num(payload.sentiment?.rollingScore) ?? base.sentiment.rollingScore,
            windowSeconds: base.sentiment.windowSeconds,
            sampleSize: num(payload.sentiment?.sampleSize) ?? 0,
          },
          participantCount: num(payload.participantCount) ?? 0,
          participants: (payload.participants ?? []) as LoungeState['participants'],
          typers: (payload.typing?.participants ?? []) as string[],
          locked: Boolean(payload.moderation?.locked),
          mutedUserIds: (payload.moderation?.mutedUserIds ?? []) as string[],
          hostDisconnected: Boolean(payload.hostDisconnected),
          mutationsFrozen: Boolean(payload.mutationsFrozen),
        },
        resync: false,
      };

    case 'lounge.queue.update': {
      const revision = num(payload.revision);
      if (revision === null) return { state: base, resync: true };
      if (revision < state.revision) return { state: base, resync: true };
      if (revision <= state.revision) return { state: base, resync: false };
      const gap = revision > state.revision + 1;
      return {
        state: {
          ...base,
          revision,
          nextUp: (payload.queue ?? []) as QueueItem[],
          // A queue vote moves nothing that is playing and says so by carrying no `nowPlaying`;
          // writing the absence would blank the record on every ballot.
          nowPlaying: 'nowPlaying' in payload ? text(payload.nowPlaying) : base.nowPlaying,
        },
        resync: gap,
      };
    }

    case 'lounge.soundclash.open': {
      const candidates = (payload.candidateTrackIds ?? []) as string[];
      const opened: Battle = {
        battleId: text(payload.battleId) ?? '',
        candidateTrackIds: candidates,
        closesAt: num(payload.closesAt) ?? 0,
        tally: candidates.map(() => 0),
      };
      return {
        state: {
          ...base,
          battle: opened,
          // Opening a battle consumes these submissions; the rest of the pool is still the
          // crowd's next challenge.
          submissions: base.submissions.filter((s) => !candidates.includes(s.trackId)),
        },
        resync: false,
      };
    }

    case 'lounge.soundclash.tally': {
      if (!base.battle || payload.battleId !== base.battle.battleId) return { state: base, resync: false };
      const counts = new Map<string, number>(
        ((payload.tally ?? []) as { trackId: string; votes: number }[]).map((t) => [t.trackId, t.votes]),
      );
      return {
        state: {
          ...base,
          battle: { ...base.battle, tally: base.battle.candidateTrackIds.map((id) => counts.get(id) ?? 0) },
        },
        resync: false,
      };
    }

    case 'lounge.soundclash.resolved': {
      if (!base.battle || payload.battleId !== base.battle.battleId) return { state: base, resync: false };
      return {
        state: {
          ...base,
          battle: null,
          lastResult: {
            battleId: text(payload.battleId) ?? '',
            winningTrackId: text(payload.winningTrackId),
          },
        },
        resync: false,
      };
    }

    case 'lounge.soundclash.submission':
      return {
        state: { ...base, submissions: (payload.submissions ?? []) as LoungeState['submissions'] },
        resync: false,
      };

    case 'lounge.chat.message':
      return {
        state: {
          ...base,
          messages: appendMessage(base, {
            id: text(payload.messageId) ?? `m${frame.ts}`,
            kind: 'chat',
            userId: text(payload.userId),
            body: text(payload.body) ?? '',
            sentiment: text(payload.sentimentBucket),
            tsSeconds: num(payload.tsSeconds),
          }),
        },
        resync: false,
      };

    case 'lounge.chat.reaction':
      return {
        state: {
          ...base,
          messages: appendMessage(base, {
            id: text(payload.messageId) ?? `r${frame.ts}`,
            kind: 'reaction',
            userId: text(payload.participantId),
            body: text(payload.emoji) ?? '',
            sentiment: null,
            tsSeconds: num(payload.tsSeconds),
          }),
        },
        resync: false,
      };

    case 'lounge.chat.typing':
      return { state: { ...base, typers: (payload.typers ?? []) as string[] }, resync: false };

    case 'lounge.sentiment.score':
      return {
        state: {
          ...base,
          sentiment: {
            rollingScore: num(payload.rollingScore) ?? base.sentiment.rollingScore,
            windowSeconds: num(payload.windowSeconds) ?? base.sentiment.windowSeconds,
            sampleSize: num(payload.sampleSize) ?? 0,
          },
        },
        resync: false,
      };

    case 'lounge.presence.update':
      return {
        state: {
          ...base,
          participantCount: num(payload.participantCount) ?? base.participantCount,
          presenceFeed: [
            { participantId: text(payload.participantId) ?? '', action: text(payload.action) ?? '' },
            ...base.presenceFeed,
          ].slice(0, PRESENCE_WINDOW),
        },
        resync: false,
      };

    case 'lounge.host.state':
      return {
        state: {
          ...base,
          hostDisconnected: Boolean(payload.hostDisconnected),
          mutationsFrozen: Boolean(payload.mutationsFrozen),
        },
        resync: false,
      };

    case 'lounge.moderation':
      return { state: moderate(base, payload), resync: false };

    case 'lounge.ai.dj_announcement':
      return {
        state: {
          ...base,
          banter: appendBanter(base, {
            id: `a${frame.ts}:${base.banter.length}`,
            kind: 'announcement',
            text: text(payload.text) ?? '',
            question: null,
            relatedTrackId: text(payload.relatedTrackId),
            sourceEvents: (payload.sourceEvents ?? []) as string[],
            declined: false,
          }),
        },
        resync: false,
      };

    case 'lounge.ai.answer':
      return {
        state: {
          ...base,
          banter: appendBanter(base, {
            id: text(payload.messageId) ?? `a${frame.ts}`,
            kind: 'answer',
            text: text(payload.text) ?? '',
            question: text(payload.question),
            relatedTrackId: text(payload.relatedTrackId),
            sourceEvents: (payload.sourceEvents ?? []) as string[],
            declined: Boolean(payload.declined),
          }),
        },
        resync: false,
      };

    case 'lounge.status.update':
      return {
        state: { ...base, status: text(payload.status) ?? base.status, endedAt: text(payload.endedAt) },
        resync: false,
      };

    case 'lounge.error':
      return {
        state: {
          ...state,
          lastError: { message: text(payload.message) ?? 'the room refused that', status: num(payload.status) },
        },
        resync: false,
      };

    case 'lounge.sync.pong': {
      const sent = num(payload.clientSentAt);
      const received = num(payload.serverReceivedAt);
      return {
        state: { ...base, serverDriftMs: sent !== null && received !== null ? received - sent : null },
        resync: false,
      };
    }

    default:
      // An event this client has no rule for says nothing about the state it does have, so it
      // changes none of it. Rendering a guess is how a screen disagrees with the room.
      return { state: base, resync: false };
  }
}

function battleFrom(value: unknown): Battle | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Body;
  const candidates = (raw.candidateTrackIds ?? []) as string[];
  const counts = new Map<string, number>(
    ((raw.tally ?? []) as { trackId: string; votes: number }[]).map((t) => [t.trackId, t.votes]),
  );
  return {
    battleId: text(raw.battleId) ?? '',
    candidateTrackIds: candidates,
    closesAt: num(raw.closesAt) ?? 0,
    tally: candidates.map((id) => counts.get(id) ?? 0),
  };
}

function moderate(state: LoungeState, payload: Body): LoungeState {
  const target = text(payload.targetUserId);
  switch (payload.action) {
    case 'muted': {
      if (target === null) return state;
      const muted = new Set(state.mutedUserIds);
      if (payload.muted === false) muted.delete(target);
      else muted.add(target);
      return { ...state, mutedUserIds: [...muted].sort() };
    }
    case 'locked':
      return { ...state, locked: Boolean(payload.locked) };
    case 'removed':
      // The removed person's own socket learns this from the frame it receives before the
      // close; everybody else learns it from here.
      return { ...state, notice: `${target ?? 'a participant'} was removed from this lounge by its host` };
    case 'transferred':
      return target === null ? state : { ...state, hostUserId: target };
    default:
      return state;
  }
}

function appendMessage(state: LoungeState, line: ChatLine): ChatLine[] {
  // The history endpoint and the live frame carry the same `messageId`, and a socket that
  // reconnects can be handed a line it already showed. A duplicate is a bug the reader sees.
  if (state.messages.some((existing) => existing.id === line.id)) return state.messages;
  return [...state.messages, line].slice(-MESSAGE_WINDOW);
}

function appendBanter(state: LoungeState, line: BanterLine): BanterLine[] {
  if (state.banter.some((existing) => existing.id === line.id)) return state.banter;
  return [...state.banter, line].slice(-BANTER_WINDOW);
}

export interface HistoryRecord {
  messageId: string;
  userId: string | null;
  msgType: string;
  body: string;
  sentimentScore: number | null;
  /** The bucket the room used when it heard the line, named by the server on read. */
  sentimentBucket: string | null;
  tsSeconds: number;
  createdAt: string;
}

/** Fold §5.1's persisted transcript into the same list the live frames append to.
 *
 * Restoring a room's conversation happens once, on join, and it goes through this rather than
 * straight into a pane because the live path can deliver a line that was already in the store —
 * a message sent while this client was fetching history is in both, and the reader should see it
 * once.
 */
export function loadHistory(state: LoungeState, records: HistoryRecord[]): LoungeState {
  const kindFor: Record<string, ChatLine['kind']> = {
    chat: 'chat',
    reaction: 'reaction',
    ai_answer: 'answer',
    dj_announcement: 'announcement',
    system: 'system',
  };
  let messages = state.messages;
  for (const record of records) {
    messages = appendMessage(
      { ...state, messages },
      {
        id: record.messageId,
        kind: kindFor[record.msgType] ?? 'system',
        userId: record.userId,
        body: record.body,
        sentiment: record.sentimentBucket,
        tsSeconds: record.tsSeconds,
      },
    );
  }
  return { ...state, messages };
}
