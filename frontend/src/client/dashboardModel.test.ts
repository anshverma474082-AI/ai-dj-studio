/** §6.3's dashboard half, tested where the lying would happen.

These functions exist because a number on a screen is a claim. §6.3 asks for an acoustic taste
profile, a ranked rail that says *why*, and session analytics that reconcile with the event log —
and every one of those has a version that is technically true and misleading. An empty history
rendered as "0–0 BPM" tells the reader they like silence. A rail that says "because you heard
Night Drive" when the collaborative half returned zero invents a basis. A sentiment line drawn
through one sample shows a trend that no data supports. So the shaping lives here, in pure
functions, with a test per claim, rather than inside a JSX expression nobody can run.

React is not in this file on purpose: Vitest here runs with `environment: 'node'`, so the panels
are checked by `tsc` and by a person looking at a browser, and these are checked by the suite.
*/
import { describe, expect, it } from 'vitest';

import {
  analyticsHeadline,
  railReason,
  railScores,
  sentimentTimeline,
  splitLounges,
  tasteCopy,
  type LoungeRow,
  type Taste,
} from './dashboardModel';

const lounge = (over: Partial<LoungeRow>): LoungeRow => ({
  loungeId: 'l1',
  inviteCode: 'SP-DEMO',
  status: 'live',
  role: 'host',
  canRejoin: true,
  endedAt: null,
  ...over,
});

describe('splitLounges', () => {
  it('keeps a room you can walk back into out of the history list', () => {
    const split = splitLounges([lounge({ loungeId: 'live1' }), lounge({ loungeId: 'past1', canRejoin: false })]);

    expect(split.open.map((row) => row.loungeId)).toEqual(['live1']);
    expect(split.past.map((row) => row.loungeId)).toEqual(['past1']);
  });

  it('keeps the order the server sent, because the dashboard is sorted by the database and not by luck', () => {
    const split = splitLounges([
      lounge({ loungeId: 'a' }),
      lounge({ loungeId: 'b', canRejoin: false }),
      lounge({ loungeId: 'c' }),
    ]);

    expect(split.open.map((row) => row.loungeId)).toEqual(['a', 'c']);
    expect(split.past.map((row) => row.loungeId)).toEqual(['b']);
  });
});

describe('tasteCopy', () => {
  const empty: Taste = { records: 0, bpmLow: null, bpmHigh: null, avgEnergy: null, preferredKeys: [] };

  it('reads a history of nothing as nothing, not as a preference for silence', () => {
    const copy = tasteCopy(empty);

    expect(copy.hasHistory).toBe(false);
    // The bug this guards is arithmetic-shaped: a null range printed through a `?? 0` becomes
    // "0 BPM", which is a tempo the listener never heard.
    expect(copy.bpm).not.toMatch(/0/);
    expect(copy.energy).not.toMatch(/0/);
    expect(copy.keys).toEqual([]);
  });

  it('sizes bars against the history, so a history of nothing has no bars', () => {
    // The share's denominator is `records`. A payload with key rows but no records heard is not
    // something the server sends today — the two come from the same join — but the division is
    // only safe while the guard is, and `Infinity` in a bar width renders a full track.
    const copy = tasteCopy({ ...empty, preferredKeys: [{ key: '8A', records: 1 }] });

    expect(copy.keys).toEqual([]);
  });

  it('prints a one-tempo history as one tempo', () => {
    const copy = tasteCopy({ ...empty, records: 2, bpmLow: 126, bpmHigh: 126 });

    expect(copy.hasHistory).toBe(true);
    expect(copy.bpm).toBe('126 BPM');
  });

  it('keeps a range a range', () => {
    const copy = tasteCopy({ ...empty, records: 4, bpmLow: 124, bpmHigh: 128 });

    expect(copy.bpm).toBe('124–128 BPM');
  });

  it('names the half it does not have instead of inventing one', () => {
    const copy = tasteCopy({ ...empty, records: 3, bpmLow: 124, bpmHigh: null });

    expect(copy.bpm).toContain('124');
    expect(copy.bpm).not.toContain('–');
  });

  it('sizes each preferred key against the records it came from', () => {
    const copy = tasteCopy({ ...empty, records: 6, preferredKeys: [{ key: '9A', records: 3 }, { key: '8A', records: 2 }] });

    // The bar's width is the number a reader eyeballs, so it has to be the share of *records
    // heard* — 3 of 6 is half, and a chart that fills the track with 3 bars would read as 3 of 3.
    expect(copy.keys[0]).toEqual({ key: '9A', records: 3, share: 0.5 });
    expect(copy.keys[1]).toEqual({ key: '8A', records: 2, share: 2 / 6 });
  });
});

describe('railReason', () => {
  it('names the record the content half points at', () => {
    const reason = railReason({
      trackId: 't_9',
      title: 'Freight Elevator',
      artist: 'Mono No Aware',
      score: 0.71,
      contentScore: 0.62,
      cfScore: 0,
      basisTrackId: 't_2001',
      basisTitle: 'Night Drive',
    });

    expect(reason).toBe('because you heard Night Drive');
  });

  it('does not credit co-listeners when the collaborative half had nothing to say', () => {
    const reason = railReason({
      trackId: 't_9',
      title: 'Freight Elevator',
      artist: 'Mono No Aware',
      score: 0.4,
      contentScore: 0.4,
      cfScore: 0,
      basisTrackId: null,
      basisTitle: null,
    });

    // A hybrid rail with `contentScore === score` was decided by acoustics alone. Saying
    // "listeners who liked X" here would be the PRD's collaborative claim with no evidence.
    expect(reason.toLowerCase()).not.toContain('heard');
    expect(reason.toLowerCase()).not.toContain('listener');
    expect(reason).toContain('acoustic');
  });

  it('says both halves when the room agreed with the acoustic pick', () => {
    const reason = railReason({
      trackId: 't_9',
      title: 'Peak Hour',
      artist: 'Sitar Circuit',
      score: 0.66,
      contentScore: 0.79,
      cfScore: 0.49,
      basisTrackId: 't_2001',
      basisTitle: 'Night Drive',
    });

    // `_basis` is the listener's own closest record, so "because you heard Night Drive" is the
    // content claim. When the collaborative score is above zero it is a second, separate claim,
    // and a reader comparing the two numbers against the sentence deserves to see that both fired.
    expect(reason).toBe('because you heard Night Drive · the room ranked it too');
  });

  it('says when the rail is not about you at all', () => {
    const reason = railReason({
      trackId: 't_9',
      title: 'Freight Elevator',
      artist: 'Mono No Aware',
      score: 0.1,
      contentScore: 0.1,
      cfScore: 0,
      basisTrackId: null,
      basisTitle: null,
    }, 'catalog');

    expect(reason.toLowerCase()).not.toContain('for you');
    expect(reason.toLowerCase()).not.toContain('you');
  });

  it('marks a popularity rail as a fallback rather than a taste match', () => {    const reason = railReason({
      trackId: 't_9',
      title: 'Freight Elevator',
      artist: 'Mono No Aware',
      score: 0.9,
      contentScore: 0,
      cfScore: 0,
      basisTrackId: null,
      basisTitle: null,
    }, 'popularity');

    expect(reason.toLowerCase()).toContain('popular');
  });
});

describe('railScores', () => {
  it('keeps both halves when the rail was ranked from the listeners own history', () => {
    const split = railScores(
      {
        trackId: 't_9',
        title: 'Peak Hour',
        artist: 'Sitar Circuit',
        score: 0.66,
        contentScore: 0.7928,
        cfScore: 0.5619,
        basisTrackId: 't_2001',
        basisTitle: 'Night Drive',
      },
      'hybrid',
    );

    expect(split).toBe('0.79 / 0.56');
  });

  it('withholds the split from a rail that was never about the listener', () => {
    // A popularity rail carries 0.0 in both halves because neither was computed, not because
    // either came out low. Drawing the pair would put two zeroes under a sentence that says the
    // first is how close the record is to what you have heard — and this reader has heard nothing.
    const split = railScores(
      {
        trackId: 't_2004',
        title: 'Slow Burn',
        artist: 'Mono No Aware',
        score: 1,
        contentScore: 0,
        cfScore: 0,
        basisTrackId: null,
        basisTitle: null,
      },
      'popularity',
    );

    expect(split).toBeNull();
  });
});

describe('sentimentTimeline', () => {
  const point = (ts: string, rollingScore: number, sampleSize: number) => ({ ts, rollingScore, sampleSize });

  it('refuses to draw a trend through one reading', () => {
    const line = sentimentTimeline([point('2026-11-04T10:00:00Z', 0.7, 3)], 320, 60);

    // A polyline needs two ends. One sample is a dot and a number, and the note has to say the
    // room only spoke three times.
    expect(line.renderable).toBe(false);
    expect(line.points).toBe('');
    expect(line.dots).toHaveLength(1);
    expect(line.note).toContain('one reading');
  });

  it('says out loud when it has nothing to plot', () => {
    const line = sentimentTimeline([], 320, 60);

    expect(line.renderable).toBe(false);
    expect(line.points).toBe('');
    expect(line.dots).toEqual([]);
    expect(line.note).toContain('no sentiment');
  });

  it('puts a flat line at both edges of the box', () => {
    const line = sentimentTimeline(
      [point('2026-11-04T10:00:00Z', 0, 2), point('2026-11-04T10:05:00Z', 1, 2)],
      320,
      60,
    );

    // §6.2's score is 0..1, so the extremes must land on the geometry's extremes or a calm room
    // looks agitated and the chart is quietly lying about its own scale.
    expect(line.points).toBe('0,60 320,0');
  });

  it('spreads readings left to right in the order the log has them', () => {
    const line = sentimentTimeline(
      [
        point('2026-11-04T10:00:00Z', 0.4, 2),
        point('2026-11-04T10:03:00Z', 0.6, 5),
        point('2026-11-04T10:09:00Z', 0.5, 8),
      ],
      320,
      60,
    );

    // Three minutes, then six. The x's are 0, 106.67 and 320 because the axis is the clock.
    expect(line.renderable).toBe(true);
    expect(line.points).toBe('0,36 106.67,24 320,30');
  });

  it('puts a reading where its clock says, not where its index says', () => {
    const line = sentimentTimeline(
      [
        point('2026-11-04T10:00:00Z', 0.3, 2),
        point('2026-11-04T10:08:00Z', 0.5, 4),
        point('2026-11-04T10:09:00Z', 0.7, 6),
      ],
      320,
      60,
    );

    // Eight minutes then one minute. Spacing by array index draws those gaps equal — 0, 160, 320
    // — and a reader would see a steady drift where the room went quiet for eight minutes and then
    // moved in the last minute of the session. On the clock the middle reading sits at 284.44 of
    // 320, which is where it actually happened.
    expect(line.points).toBe('0,42 284.44,30 320,18');
  });

  it('keeps a series that shares one instant off the vertical axis', () => {
    const line = sentimentTimeline(
      [point('2026-11-04T10:00:00Z', 0.4, 2), point('2026-11-04T10:00:00Z', 0.6, 4)],
      320,
      60,
    );

    // §9.4 writes a batch in one transaction, so a whole session's ticks can carry the same
    // timestamp. The clock then has no span to divide by, and a `NaN` in a polyline drops the
    // figure rather than showing it. Even spacing is the honest fallback: the order is real, the
    // gap is not.
    expect(line.renderable).toBe(true);
    expect(line.points).toBe('0,36 320,24');
  });

  it('carries its own thinness', () => {
    const line = sentimentTimeline(
      [point('2026-11-04T10:00:00Z', 0.4, 2), point('2026-11-04T10:03:00Z', 0.6, 9)],
      320,
      60,
    );

    // `sampleSize` is the reason a reader should trust the shape, so it belongs in the note.
    expect(line.note).toContain('9');
  });
});

describe('analyticsHeadline', () => {
  it('does not report a length for a session that never opened', () => {
    const headline = analyticsHeadline({ status: 'scheduled', startedAt: null, durationSeconds: null });

    // `durationSeconds` is null *because* there is no start row to measure from, and turning that
    // into 0 would print "0:00" — a session that happened and was empty, rather than none.
    expect(headline).toContain('never opened');
    expect(headline).not.toContain('0:00');
  });

  it('reports the length the server measured for a session that ran', () => {
    const headline = analyticsHeadline({
      status: 'ended',
      startedAt: '2026-11-04T10:00:00Z',
      durationSeconds: 224,
    });

    expect(headline).toContain('3:44');
  });

  it('keeps a live session counted as live', () => {
    const headline = analyticsHeadline({
      status: 'live',
      startedAt: '2026-11-04T10:00:00Z',
      durationSeconds: null,
    });

    expect(headline).toContain('still going');
  });
});
