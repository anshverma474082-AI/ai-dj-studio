/** §6.1's acceptance criteria are the client's job too, and this file is where they are decided.
 *
 * The reducer is the only part of the console that can be tested without a browser: it takes the
 * frames `app/gateway/*.py` actually sends (the payload objects below are copied from those
 * builders, not invented) and turns them into the state six panels render. Keeping it pure is the
 * point — every rule below is a rule about what a screen is allowed to show, and none of them need
 * a DOM to prove.
 *
 * The three that come straight out of the PRD:
 *   - "reorder reflected identically on all clients" — a client that renders an older revision over
 *     a newer one is a client that disagrees with the room, so the revision guard is a test, not a
 *     comment.
 *   - "a mid-session joiner gets the full state" — the snapshot is applied whole, and a joiner
 *     never has to merge deltas it has no baseline for.
 *   - "clearly indicated" host state — the freeze arrives on two paths (a frame for clients already
 *     in the room, the snapshot for anybody who joins after it) and both must reach the same field.
 */
import { describe, expect, it } from 'vitest';

import { applyFrame, initialState, loadHistory, type Frame, type LoungeState } from './loungeReducer';

const LOUNGE = 'lg_demo';

function frame(event: string, payload: unknown): Frame {
  return { event, loungeId: LOUNGE, payload, ts: 1_730_000_000_000 };
}

function apply(state: LoungeState, event: string, payload: unknown) {
  return applyFrame(state, frame(event, payload));
}

function reduced(state: LoungeState, event: string, payload: unknown): LoungeState {
  return apply(state, event, payload).state;
}

const SNAPSHOT = {
  loungeId: LOUNGE,
  revision: 7,
  hostUserId: 'u-host',
  nowPlaying: 't_2001',
  nextUp: [
    { trackId: 't_2002', position: 0, voteCount: 3 },
    { trackId: 't_2003', position: 1000, voteCount: 0 },
  ],
  soundclash: null,
  soundclashSubmissions: [{ trackId: 't_2004', submittedByUserId: 'u-listener' }],
  sentiment: { rollingScore: 0.62, sampleSize: 9 },
  participantCount: 4,
  hostDisconnected: false,
  mutationsFrozen: false,
  moderation: { locked: false, mutedUserIds: [] },
  typing: { participants: ['u-co-dj'], expiresInSeconds: 6 },
};

describe('lounge.snapshot', () => {
  it('is applied whole, because a joiner has no baseline to merge deltas into', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    expect(state.revision).toBe(7);
    expect(state.hostUserId).toBe('u-host');
    expect(state.nowPlaying).toBe('t_2001');
    expect(state.nextUp.map((item) => item.trackId)).toEqual(['t_2002', 't_2003']);
    expect(state.submissions).toEqual([{ trackId: 't_2004', submittedByUserId: 'u-listener' }]);
    expect(state.participantCount).toBe(4);
    expect(state.typers).toEqual(['u-co-dj']);
    expect(state.sentiment.rollingScore).toBeCloseTo(0.62);
  });

  it('carries the lifecycle word, because a joiner missed the frame that announced it', () => {
    // `lounge.status.update` fires once, when the room opens or closes. A tab that opens after
    // it would otherwise sit on "status pending" for the whole session, so the snapshot has to
    // answer the same question the announcement did.
    const state = reduced(initialState, 'lounge.snapshot', { ...SNAPSHOT, status: 'live' });
    expect(state.status).toBe('live');
  });

  it('names the people seated, because the host controls take a user id', () => {
    const state = reduced(initialState, 'lounge.snapshot', {
      ...SNAPSHOT,
      participants: [
        { userId: 'u-host', role: 'host' },
        { userId: 'u-listener', role: 'listener' },
      ],
    });
    expect(state.participants).toEqual([
      { userId: 'u-host', role: 'host' },
      { userId: 'u-listener', role: 'listener' },
    ]);
  });

  it('carries the host freeze to the same field the live frame uses', () => {
    const joined = reduced(initialState, 'lounge.snapshot', {
      ...SNAPSHOT,
      hostDisconnected: true,
      mutationsFrozen: true,
    });
    expect(joined.mutationsFrozen).toBe(true);
    expect(joined.hostDisconnected).toBe(true);
  });

  it('carries an open battle so a joiner sees the count the room is at', () => {
    const joined = reduced(initialState, 'lounge.snapshot', {
      ...SNAPSHOT,
      soundclash: {
        battleId: 'b1',
        candidateTrackIds: ['t_2002', 't_2003'],
        closesAt: 1_730_000_030_000,
        tally: [
          { trackId: 't_2002', votes: 3 },
          { trackId: 't_2003', votes: 1 },
        ],
      },
    });
    expect(joined.battle).toMatchObject({ battleId: 'b1', tally: [3, 1] });
  });

  it('carries the moderation state a joiner cannot discover by trying it', () => {
    const joined = reduced(initialState, 'lounge.snapshot', {
      ...SNAPSHOT,
      moderation: { locked: true, mutedUserIds: ['u-noisy'] },
    });
    expect(joined.locked).toBe(true);
    expect(joined.mutedUserIds).toEqual(['u-noisy']);
  });

  it('clears a battle the room no longer has', () => {
    const open = reduced(joined2(), 'lounge.snapshot', SNAPSHOT);
    expect(open.battle).toBeNull();
    expect(open.lastResult).toBeNull();
  });
});

function joined2(): LoungeState {
  return reduced(initialState, 'lounge.snapshot', {
    ...SNAPSHOT,
    soundclash: {
      battleId: 'b1',
      candidateTrackIds: ['t_2002', 't_2003'],
      closesAt: 1_730_000_030_000,
      tally: [
        { trackId: 't_2002', votes: 1 },
        { trackId: 't_2003', votes: 0 },
      ],
    },
  });
}

describe('lounge.queue.update', () => {
  it('replaces the order and the current record when the revision moves forward', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const updated = reduced(state, 'lounge.queue.update', {
      queue: [
        { trackId: 't_2003', position: 0, voteCount: 4 },
        { trackId: 't_2002', position: 1000, voteCount: 3 },
      ],
      nowPlaying: 't_2001',
      reason: 'vote',
      revision: 8,
    });
    expect(updated.nextUp.map((item) => item.trackId)).toEqual(['t_2003', 't_2002']);
    expect(updated.revision).toBe(8);
  });

  it('leaves Now Playing alone when the frame does not move it', () => {
    // `_handle_vote` sends {queue, reason, revision} and no nowPlaying: a queue vote moves
    // nothing that is playing. A reducer that wrote `undefined` here would blank the record
    // every time anybody clicked vote.
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const updated = reduced(state, 'lounge.queue.update', {
      queue: [{ trackId: 't_2002', position: 0, voteCount: 4 }],
      reason: 'vote',
      revision: 8,
    });
    expect(updated.nowPlaying).toBe('t_2001');
  });

  it('adopts the nowPlaying a segue carries', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const updated = reduced(state, 'lounge.queue.update', {
      queue: [{ trackId: 't_2003', position: 0, voteCount: 0 }],
      nowPlaying: 't_2002',
      reason: 'segue',
      revision: 8,
    });
    expect(updated.nowPlaying).toBe('t_2002');
  });

  it('drops an update whose revision it already has', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const newer = reduced(state, 'lounge.queue.update', {
      queue: [{ trackId: 't_2003', position: 0, voteCount: 9 }],
      reason: 'vote',
      revision: 8,
    });
    const stale = reduced(newer, 'lounge.queue.update', {
      queue: [{ trackId: 't_2002', position: 0, voteCount: 1 }],
      reason: 'vote',
      revision: 8,
    });
    expect(stale.nextUp[0]?.trackId).toBe('t_2003');
  });

  it('asks for a snapshot when a revision arrives out of the order the room published it', () => {
    // The guard is not enough on its own: dropping the older frame is right, but a client that
    // missed a beat has a queue it cannot prove is current. §6.1's answer is the resync, and this
    // is the test that makes the hook actually perform it.
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const { resync } = apply(state, 'lounge.queue.update', {
      queue: [{ trackId: 't_2002', position: 0, voteCount: 1 }],
      reason: 'vote',
      revision: 5,
    });
    expect(resync).toBe(true);
  });

  it('asks for a snapshot when it jumps past a revision the room must have published', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const { resync } = apply(state, 'lounge.queue.update', {
      queue: [],
      reason: 'vote',
      revision: 12,
    });
    expect(resync).toBe(true);
  });
});

describe('lounge.soundclash.*', () => {
  it('opens a battle with its candidates and a countdown the client can render', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const opened = reduced(state, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003', 't_2004'],
      closesAt: 1_730_000_030_000,
    });
    expect(opened.battle).toMatchObject({
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003', 't_2004'],
      closesAt: 1_730_000_030_000,
    });
    // Every candidate starts at zero on the screen, so a joiner and a voter see the same 0-0.
    expect(opened.battle?.tally).toEqual([0, 0, 0]);
    expect(opened.submissions).toEqual([]);
  });

  it('replaces the tally with the count the room holds rather than adding to it', () => {
    // The server publishes the whole tally per ballot (state.battle_tally), so a client that
    // summed the frames would count every earlier ballot again.
    const opened = reduced(initialState, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003'],
      closesAt: 1_730_000_030_000,
    });
    const tallied = reduced(opened, 'lounge.soundclash.tally', {
      battleId: 'b9',
      participantId: 'u-listener',
      trackId: 't_2002',
      tally: [
        { trackId: 't_2002', votes: 2 },
        { trackId: 't_2003', votes: 1 },
      ],
    });
    expect(tallied.battle?.tally).toEqual([2, 1]);
  });

  it('ignores a tally for a battle the room is not running', () => {
    const opened = reduced(initialState, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003'],
      closesAt: 1_730_000_030_000,
    });
    const stale = reduced(opened, 'lounge.soundclash.tally', {
      battleId: 'b-old',
      participantId: 'u-listener',
      trackId: 't_2002',
      tally: [
        { trackId: 't_2002', votes: 40 },
        { trackId: 't_2003', votes: 0 },
      ],
    });
    expect(stale.battle?.tally).toEqual([0, 0]);
  });

  it('clears the stage when the room opens a battle over it', () => {
    // `close_battle` consumes the chosen submissions; a stage that still lists them invites a
    // duplicate challenge for a track already being voted on.
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const opened = reduced(state, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2004'],
      closesAt: 1_730_000_030_000,
    });
    expect(opened.submissions).toEqual([]);
  });

  it('records a resolution and closes the third zone', () => {
    const opened = reduced(initialState, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003'],
      closesAt: 1_730_000_030_000,
    });
    const resolved = reduced(opened, 'lounge.soundclash.resolved', {
      battleId: 'b9',
      winningTrackId: 't_2002',
    });
    expect(resolved.battle).toBeNull();
    expect(resolved.lastResult).toEqual({ battleId: 'b9', winningTrackId: 't_2002' });
  });

  it('keeps a stalemate visible as a stalemate', () => {
    const opened = reduced(initialState, 'lounge.soundclash.open', {
      battleId: 'b9',
      candidateTrackIds: ['t_2002', 't_2003'],
      closesAt: 1_730_000_030_000,
    });
    const resolved = reduced(opened, 'lounge.soundclash.resolved', {
      battleId: 'b9',
      winningTrackId: null,
    });
    expect(resolved.lastResult?.winningTrackId).toBeNull();
  });

  it('replaces the whole submission pool when one lands', () => {
    // `_handle_soundclash_submit` sends the pool, not the addition, so a client that missed a
    // submission still renders the stage the room holds.
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const withTwo = reduced(state, 'lounge.soundclash.submission', {
      trackId: 't_2005',
      submittedByUserId: 'u-other',
      submissions: [
        { trackId: 't_2004', submittedByUserId: 'u-listener' },
        { trackId: 't_2005', submittedByUserId: 'u-other' },
      ],
    });
    expect(withTwo.submissions.map((s) => s.trackId)).toEqual(['t_2004', 't_2005']);
  });
});

describe('chat, reactions and typing', () => {
  it('appends a message with the sentiment bucket the server scored it with', () => {
    const state = reduced(initialState, 'lounge.chat.message', {
      messageId: 'm1',
      userId: 'u-listener',
      body: 'this transition is *chef kiss*',
      sentimentBucket: 'positive',
      tsSeconds: 42,
    });
    expect(state.messages).toEqual([
      {
        id: 'm1',
        kind: 'chat',
        userId: 'u-listener',
        body: 'this transition is *chef kiss*',
        sentiment: 'positive',
        tsSeconds: 42,
      },
    ]);
  });

  it('never shows the same message twice when history and the live frame both carry it', () => {
    const withHistory = loadHistory(initialState, [
      {
        messageId: 'm1',
        userId: 'u-listener',
        msgType: 'chat',
        body: 'early',
        sentimentScore: 0.8,
        sentimentBucket: 'positive',
        tsSeconds: 12,
        createdAt: '2026-11-01T10:00:00Z',
      },
    ]);
    const alsoLive = reduced(withHistory, 'lounge.chat.message', {
      messageId: 'm1',
      userId: 'u-listener',
      body: 'early',
      sentimentBucket: 'positive',
      tsSeconds: 12,
    });
    expect(alsoLive.messages).toHaveLength(1);
  });

  it('hydrates a replayed line with the bucket word a live line would have shown', () => {
    // One message, two sources. The live frame carries `sentimentBucket`; history used to
    // carry only the score, so the feed printed "0.7512" for a line the room had just
    // called "positive". The server now names the bucket from the stored score instead of
    // letting the client re-derive it, so this asserts the word, not the number.
    const state = loadHistory(initialState, [
      {
        messageId: 'm9',
        userId: 'u-listener',
        msgType: 'chat',
        body: 'this set is amazing',
        sentimentScore: 0.7512,
        sentimentBucket: 'positive',
        tsSeconds: 8,
        createdAt: '2026-11-01T10:00:00Z',
      },
    ]);
    expect(state.messages[0]?.sentiment).toBe('positive');
  });

  it('renders a reaction as a reaction, with the sender the frame names', () => {
    const state = reduced(initialState, 'lounge.chat.reaction', {
      messageId: 'r1',
      participantId: 'u-co-dj',
      emoji: '🔥',
      sentimentScore: 0.9,
      tsSeconds: 30,
    });
    expect(state.messages[0]).toMatchObject({ kind: 'reaction', userId: 'u-co-dj', body: '🔥' });
  });

  it('adopts the typers list the room publishes instead of merging a delta', () => {
    // `typing_frame` sends the whole room's typers on every change, which is what lets two
    // screens agree. A client that added and removed names itself would be the disagreement.
    const first = reduced(initialState, 'lounge.chat.typing', {
      participantId: 'u-a',
      isTyping: true,
      typers: ['u-a'],
      expiresInSeconds: 6,
    });
    const second = reduced(first, 'lounge.chat.typing', {
      participantId: 'u-b',
      isTyping: true,
      typers: ['u-a', 'u-b'],
      expiresInSeconds: 6,
    });
    expect(second.typers).toEqual(['u-a', 'u-b']);
    const stopped = reduced(second, 'lounge.chat.typing', {
      participantId: 'u-a',
      isTyping: false,
      typers: ['u-b'],
      expiresInSeconds: 6,
    });
    expect(stopped.typers).toEqual(['u-b']);
  });

  it('keeps the conversation inside a bounded window', () => {
    let state = initialState;
    for (let i = 0; i < 400; i += 1) {
      state = reduced(state, 'lounge.chat.message', {
        messageId: `m${i}`,
        userId: 'u-x',
        body: `line ${i}`,
        sentimentBucket: 'neutral',
        tsSeconds: i,
      });
    }
    expect(state.messages.length).toBeLessThanOrEqual(200);
    expect(state.messages.at(-1)?.body).toBe('line 399');
  });
});

describe('the room\'s mood and its people', () => {
  it('reads the rolling score, its window and how thin the sample is', () => {
    const state = reduced(initialState, 'lounge.sentiment.score', {
      rollingScore: 0.4123,
      windowSeconds: 45,
      sampleSize: 3,
    });
    expect(state.sentiment).toEqual({ rollingScore: 0.4123, windowSeconds: 45, sampleSize: 3 });
  });

  it('takes the participant count the room reports and keeps the comings and goings', () => {
    const joined = reduced(
      reduced(initialState, 'lounge.snapshot', SNAPSHOT),
      'lounge.presence.update',
      { participantId: 'u-new', action: 'joined', participantCount: 5 },
    );
    expect(joined.participantCount).toBe(5);
    expect(joined.presenceFeed[0]).toEqual({ participantId: 'u-new', action: 'joined' });
    const left = reduced(joined, 'lounge.presence.update', {
      participantId: 'u-new',
      action: 'left',
      participantCount: 4,
    });
    expect(left.participantCount).toBe(4);
    expect(left.presenceFeed.map((entry) => entry.action)).toEqual(['left', 'joined']);
  });
});

describe('host state and moderation', () => {
  it('shows the freeze the room just entered, and clears it when it lifts', () => {
    const frozen = reduced(initialState, 'lounge.host.state', {
      hostDisconnected: true,
      mutationsFrozen: true,
    });
    expect(frozen.mutationsFrozen).toBe(true);
    const thawed = reduced(frozen, 'lounge.host.state', {
      hostDisconnected: false,
      mutationsFrozen: false,
    });
    expect(thawed.mutationsFrozen).toBe(false);
    expect(thawed.hostDisconnected).toBe(false);
  });

  it('folds a mute into the list every screen agrees on', () => {
    const muted = reduced(initialState, 'lounge.moderation', {
      action: 'muted',
      targetUserId: 'u-noisy',
      muted: true,
    });
    expect(muted.mutedUserIds).toEqual(['u-noisy']);
    const cleared = reduced(muted, 'lounge.moderation', {
      action: 'muted',
      targetUserId: 'u-noisy',
      muted: false,
    });
    expect(cleared.mutedUserIds).toEqual([]);
  });

  it('keeps the muted list in one order so two renders match', () => {
    let state = initialState;
    for (const target of ['u-z', 'u-a', 'u-m']) {
      state = reduced(state, 'lounge.moderation', { action: 'muted', targetUserId: target, muted: true });
    }
    expect(state.mutedUserIds).toEqual(['u-a', 'u-m', 'u-z']);
  });

  it('shows the lock a client cannot otherwise see', () => {
    const locked = reduced(initialState, 'lounge.moderation', { action: 'locked', locked: true });
    expect(locked.locked).toBe(true);
    const open = reduced(locked, 'lounge.moderation', { action: 'locked', locked: false });
    expect(open.locked).toBe(false);
  });

  it('moves the host when the room is handed over', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const transferred = reduced(state, 'lounge.moderation', {
      action: 'transferred',
      targetUserId: 'u-co-dj',
    });
    expect(transferred.hostUserId).toBe('u-co-dj');
  });

  it('says who was put out of the room', () => {
    const removed = reduced(initialState, 'lounge.moderation', {
      action: 'removed',
      targetUserId: 'u-noisy',
    });
    expect(removed.notice).toBe('u-noisy was removed from this lounge by its host');
  });
});

describe('the DJ talking', () => {
  it('appends a transition line with the record it is about and what grounds it', () => {
    const state = reduced(initialState, 'lounge.ai.dj_announcement', {
      text: 'Next up: a 128 BPM house track two hours later in the same key.',
      relatedTrackId: 't_2002',
      sourceEvents: ['e1', 'e2'],
    });
    expect(state.banter[0]).toMatchObject({
      kind: 'announcement',
      text: 'Next up: a 128 BPM house track two hours later in the same key.',
      relatedTrackId: 't_2002',
      sourceEvents: ['e1', 'e2'],
    });
  });

  it('files an answer under the question it replies to, and marks a decline', () => {
    const state = reduced(initialState, 'lounge.ai.answer', {
      messageId: 'a1',
      question: 'why this track?',
      text: 'I do not have that in this room’s notes.',
      relatedTrackId: 't_2001',
      sourceEvents: [],
      declined: true,
    });
    expect(state.banter[0]).toMatchObject({ kind: 'answer', question: 'why this track?', declined: true });
  });
});

describe('the session ending, and being refused', () => {
  it('marks the lounge ended with the server’s own timestamp', () => {
    const state = reduced(reduced(initialState, 'lounge.snapshot', SNAPSHOT), 'lounge.status.update', {
      status: 'ended',
      endedAt: '2026-11-01T10:30:00+00:00',
    });
    expect(state.status).toBe('ended');
    expect(state.endedAt).toBe('2026-11-01T10:30:00+00:00');
  });

  it('surfaces a refusal without wrecking the state it refused on', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const refused = reduced(state, 'lounge.error', {
      message: 'queue is frozen: the host disconnected and votes resume when they reconnect',
      status: 403,
    });
    expect(refused.lastError?.message).toContain('queue is frozen');
    expect(refused.lastError?.status).toBe(403);
    expect(refused.nextUp).toEqual(state.nextUp);
  });

  it('clears the previous refusal when the room answers something else', () => {
    const refused = reduced(initialState, 'lounge.error', { message: 'nope', status: 422 });
    const next = reduced(refused, 'lounge.chat.message', {
      messageId: 'm1',
      userId: 'u-x',
      body: 'fine then',
      sentimentBucket: 'neutral',
      tsSeconds: 3,
    });
    expect(next.lastError).toBeNull();
  });

  it('reads round-trip latency off the pong it asked for', () => {
    const state = reduced(initialState, 'lounge.sync.pong', {
      clientSentAt: 1_730_000_000_000,
      serverReceivedAt: 1_730_000_000_012,
      revision: 7,
    });
    expect(state.serverDriftMs).toBe(12);
  });

  it('ignores an event it has no rule for rather than corrupting the state', () => {
    const state = reduced(initialState, 'lounge.snapshot', SNAPSHOT);
    const unknown = reduced(state, 'lounge.future.thing', { nonsense: true });
    expect(unknown.revision).toBe(state.revision);
    expect(unknown.nextUp).toEqual(state.nextUp);
  });
});
