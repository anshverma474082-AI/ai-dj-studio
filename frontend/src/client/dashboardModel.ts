/** §6.3's dashboard arithmetic, kept out of the panels so it can be asserted.

A dashboard is where a project gets to lie politely: every figure here is a number the server
already stands behind, and the only thing this module adds is how it reads to a person. So the
rules it encodes are all the same rule — *a missing value must not be drawn as a zero, and a
reason must not be claimed without the field that supports it.* The taste profile returns `null`
rather than `0` for an empty history precisely so a client can say "nothing on record"; rendering
that `null` through `?? 0` would tell someone their favourite tempo is silence.

`splitLounges` and the timeline mapping are here for the same reason in milder form: one place to
decide, and a test that can name the mistake.
*/

export interface LoungeRow {
  loungeId: string;
  inviteCode: string;
  status: string;
  /** "host" or "attended" — the server's word for how this caller knows the room. */
  role: string;
  canRejoin: boolean;
  endedAt: string | null;
}

export interface KeyCount {
  key: string;
  records: number;
}

export interface Taste {
  records: number;
  bpmLow: number | null;
  bpmHigh: number | null;
  avgEnergy: number | null;
  preferredKeys: KeyCount[];
}

export interface RailItem {
  trackId: string;
  title: string;
  artist: string;
  score: number;
  contentScore: number;
  cfScore: number;
  basisTrackId: string | null;
  basisTitle: string | null;
}

export interface SentimentReading {
  ts: string;
  rollingScore: number;
  sampleSize: number;
}

export interface LoungeSplit {
  open: LoungeRow[];
  past: LoungeRow[];
}

export function splitLounges(lounges: LoungeRow[]): LoungeSplit {
  return {
    open: lounges.filter((row) => row.canRejoin),
    past: lounges.filter((row) => !row.canRejoin),
  };
}

export interface TasteCopy {
  hasHistory: boolean;
  bpm: string;
  energy: string;
  keys: { key: string; records: number; share: number }[];
}

export function tasteCopy(taste: Taste): TasteCopy {
  const heard = taste.records > 0;

  return {
    hasHistory: heard,
    bpm: bpmCopy(taste, heard),
    energy: taste.avgEnergy === null ? 'no energy on record' : `energy ${Math.round(taste.avgEnergy * 100)}%`,
    // A bar is sized by its share of the records heard, so 3 of 6 is half a track and not a full
    // one. Dividing by the loudest key instead would make whichever key topped the list always
    // read as 100% of the listener's taste.
    keys: heard ? taste.preferredKeys.map((entry) => ({ ...entry, share: entry.records / taste.records })) : [],
  };
}

function bpmCopy(taste: Taste, heard: boolean): string {
  if (!heard) return 'nothing on record yet';
  const { bpmLow: low, bpmHigh: high } = taste;
  if (low === null && high === null) return 'no tempo on record';
  if (low !== null && high !== null) return low === high ? `${low} BPM` : `${low}–${high} BPM`;
  if (low !== null) return `${low} BPM and up`;
  return `up to ${high} BPM`;
}

/** Why this record is on the rail, in as many words as the payload can support. */
export function railReason(item: RailItem, strategy = 'hybrid'): string {
  // The server only fills `basisTitle` on a hybrid rail (`_fallback_rail` leaves it `None`), so
  // these two read as a guard against a payload that cannot arrive today. They come first because
  // they are the stronger claim: a catalog-wide rail is never about this listener.
  if (strategy === 'catalog') return 'the catalog, ranked — no room has played these yet';
  if (strategy === 'popularity') return 'the most popular records in the catalog';
  if (item.basisTitle === null) return 'closest acoustically to what is already queued';
  if (item.cfScore > 0) return `because you heard ${item.basisTitle} · the room ranked it too`;
  return `because you heard ${item.basisTitle}`;
}

/** Whether this rail was ranked from this listener's own history. */
export function railIsPersonal(strategy: string): boolean {
  return strategy !== 'catalog' && strategy !== 'popularity';
}

/** The content / collaborative pair under a rail row, or nothing when neither was measured.

A popularity or catalog rail reports both halves at exactly 0.0 because the hybrid model never ran
for that listener — the server ranked the catalog by crowd plays instead. A zero is a number the
reader will compare against the row above it, so an unmeasured half is left out rather than drawn.
*/
export function railScores(item: RailItem, strategy = 'hybrid'): string | null {
  if (!railIsPersonal(strategy)) return null;
  return `${item.contentScore.toFixed(2)} / ${item.cfScore.toFixed(2)}`;
}

export interface Timeline {
  renderable: boolean;
  /** The `points` attribute of a polyline; empty when there is no line to draw. */
  points: string;
  dots: { x: number; y: number }[];
  note: string;
}

export function sentimentTimeline(readings: SentimentReading[], width: number, height: number): Timeline {
  if (readings.length === 0) {
    return { renderable: false, points: '', dots: [], note: 'no sentiment was recorded for this session' };
  }

  // §6.2's rolling score is 0..1 and SVG y grows downward, so the mapping inverts.
  const plotted = readings.map((reading) => ({
    time: Date.parse(reading.ts),
    y: (1 - reading.rollingScore) * height,
  }));
  const first = plotted[0]?.time ?? 0;
  const last = plotted[plotted.length - 1]?.time ?? first;
  const span = last - first;
  const dots = plotted.map(
    (entry, index) => ({ x: dotX(index, plotted.length, entry.time, first, span, width), y: entry.y }),
  );
  const samples = readings.reduce((loudest, reading) => Math.max(loudest, reading.sampleSize), 0);

  if (plotted.length === 1) {
    const only = plotted[0];
    return {
      renderable: false,
      points: '',
      dots: [{ x: width / 2, y: only === undefined ? height / 2 : only.y }],
      note: `one reading, ${samples} messages`,
    };
  }

  return {
    renderable: true,
    points: dots.map((dot) => `${round(dot.x)},${round(dot.y)}`).join(' '),
    dots,
    note: `${readings.length} readings, up to ${samples} messages behind one`,
  };
}

export interface AnalyticsHeadline {
  status: string;
  startedAt: string | null;
  durationSeconds: number | null;
}

/** The one line a lounge row shows about its session, and the one place a `null` is explained. */
export function analyticsHeadline(analytics: AnalyticsHeadline): string {
  if (analytics.startedAt === null) return 'never opened';
  if (analytics.status === 'live') return 'still going';
  const seconds = Math.max(0, Math.round(analytics.durationSeconds ?? 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/** Where each reading sits on the horizontal axis, from its own clock.

Spacing by array index is the tempting version and it is a lie: three readings taken at
10:00, 10:08 and 10:09 become three equal gaps, and a room that went quiet for eight minutes and
then turned in the last minute reads as a steady drift.

Readings that share a timestamp land on the same x. That is not a defect to smooth over — §9.4
writes a whole batch of events in one transaction, so two sentiment ticks can genuinely carry the
same instant, and a chart that invents a gap between them would be drawing an order the log does not
claim. Only the *whole* series collapsing to one instant is unusable, and that falls back to even
spacing rather than dividing by zero.
*/
function dotX(index: number, count: number, time: number, first: number, span: number, width: number): number {
  if (count < 2) return width / 2;
  if (!(span > 0)) return (index / (count - 1)) * width;
  return ((time - first) / span) * width;
}
