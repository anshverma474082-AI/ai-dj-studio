/** The REST side of the console — the calls that are not room traffic.
 *
 * Everything a *client* could be trusted with is here, and nothing a client decides: roles,
 * permissions and legality are re-checked by the server on every request and every frame (§10.1).
 * This module's job is to carry the access token, to turn a 401 into one refresh attempt rather
 * than a dead form, and to give the caller a readable reason when the server refused.
 *
 * The tokens live in module memory on purpose. §10.1's input-validation clause is really about
 * stored XSS, and a bearer token in `localStorage` is the payload that turns one reflected bug
 * into a stolen session that outlives the tab. A reload costs a login; local demo accounts are on
 * the development form, and that is a cheaper mistake than a persistent token in a place any
 * script can read.
 */

export type Role = 'host' | 'co_dj' | 'listener' | 'admin';

export interface Track {
  id: string;
  title: string;
  artist: string;
  durationSeconds: number;
  bpm: number;
  camelotKey: string;
  energy: number;
  valence: number;
  /** Where the sound lives (`/media/<name>.wav`). Null for a record whose audio was never
   *  synthesised, which is why the player says "no recording" rather than failing silently. */
  audioAssetUrl: string | null;
}

export interface User {
  id: string;
  email: string;
  displayName: string;
  role: Role;
}

export interface Tokens {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
  expiresIn: number;
}

export interface LoungeStateOut {
  loungeId: string;
  status: string;
  hostUserId: string;
  nowPlaying: string | null;
  nextUp: { trackId: string; position: number; voteCount: number }[];
  participantCount: number;
  revision: number;
}

export interface ChatRecord {
  messageId: string;
  userId: string | null;
  msgType: string;
  body: string;
  sentimentScore: number | null;
  sentimentBucket: string | null;
  tsSeconds: number;
  createdAt: string;
}

export interface MatchReason {
  bpmDeltaPct: number;
  camelotRelation: string;
  energyFit: number;
}

/** §6.3's personal dashboard. The taste block is `null` inside rather than zero-filled, and the
 *  client's job is to keep that difference visible — `client/dashboardModel.ts` does. */
export interface LoungeSummary {
  loungeId: string;
  inviteCode: string;
  status: string;
  /** "host" or "attended", the server's word for how this caller knows the room. */
  role: string;
  canRejoin: boolean;
  endedAt: string | null;
}

export interface TasteProfile {
  records: number;
  bpmLow: number | null;
  bpmHigh: number | null;
  avgEnergy: number | null;
  preferredKeys: { key: string; records: number }[];
}

export interface Dashboard {
  userId: string;
  lounges: LoungeSummary[];
  taste: TasteProfile;
}

export interface RecommendationItem {
  trackId: string;
  title: string;
  artist: string;
  score: number;
  contentScore: number;
  cfScore: number;
  basisTrackId: string | null;
  basisTitle: string | null;
}

export interface Recommendations {
  userId: string;
  /** `hybrid`, `popularity` or `catalog` — the rail's own admission of what decided it. */
  strategy: string;
  items: RecommendationItem[];
}

export interface SentimentPoint {
  ts: string;
  rollingScore: number;
  sampleSize: number;
}

export interface SessionAnalytics {
  loungeId: string;
  status: string;
  startedAt: string | null;
  endedAt: string | null;
  durationSeconds: number | null;
  peakParticipants: number;
  battlesHeld: number;
  aiQuestionsAsked: number;
  sentimentTimeline: SentimentPoint[];
}

export interface SegueResponse {
  selectedTrackId: string | null;
  matchReason: MatchReason | null;
  transitionScript: string | null;
  sourceEvents: string[];
  generatedAt: string;
  declineReason: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let tokens: Tokens | null = null;
let user: User | null = null;

export function currentUser(): User | null {
  return user;
}

export function accessToken(): string | null {
  return tokens?.accessToken ?? null;
}

export function clearSession(): void {
  tokens = null;
  user = null;
}

async function call<T>(path: string, init: RequestInit, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set('content-type', 'application/json');
  // The access token goes in the header, never in a query string. The socket is the one place
  // a browser cannot set a header, and `useLoungeSocket` pays for that with a short-lived token
  // in the URL — which is also why the server never logs it.
  if (tokens) headers.set('authorization', `Bearer ${tokens.accessToken}`);

  const response = await fetch(path, { ...init, headers });

  if (response.status === 401 && retry && tokens?.refreshToken) {
    // One attempt, then the caller is looking at a real refusal. A loop here would turn an
    // expired session into a hung form.
    const fresh = await post<Tokens>('/api/v1/auth/refresh', { refreshToken: tokens.refreshToken }, false);
    tokens = fresh;
    return call<T>(path, init, false);
  }

  if (!response.ok) throw new ApiError(response.status, await reason(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function reason(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;
    if (typeof detail === 'string') return detail;
    // FastAPI's 422 is a list of shaped errors; the first line is the one worth showing.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(first?.loc) ? first.loc.at(-1) : undefined;
      return `${field ?? 'request'}: ${first?.msg ?? 'invalid'}`;
    }
    return `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function post<T>(path: string, body: unknown, retry = true): Promise<T> {
  return call<T>(path, { method: 'POST', body: JSON.stringify(body) }, retry);
}

export async function login(email: string, password: string): Promise<User> {
  tokens = await post<Tokens>('/api/v1/auth/login', { email, password });
  user = await get<User>('/api/v1/auth/me');
  return user;
}

export function logout(): void {
  clearSession();
}

export function get<T>(path: string): Promise<T> {
  return call<T>(path, { method: 'GET' });
}

export const listTracks = () => get<Track[]>('/api/v1/tracks');

export const createLounge = (body: {
  seedTrackId: string;
  vibeProfile?: string;
  maxParticipants?: number;
  bpmTolerancePct?: number;
  soundclashEnabled?: boolean;
  aiVerbosity?: string;
}) => post<{ loungeId: string; inviteCode: string; status: string; hostUserId: string }>('/api/v1/lounges', body);

export const joinLounge = (loungeId: string) =>
  post<LoungeStateOut>(`/api/v1/lounges/${loungeId}/join`, {});

export const readLounge = (loungeId: string) => get<LoungeStateOut>(`/api/v1/lounges/${loungeId}`);

export const endLounge = (loungeId: string) =>
  post<{ loungeId: string; status: string; endedAt: string }>(`/api/v1/lounges/${loungeId}/end`, {});

export const chatHistory = (loungeId: string, limit = 80) =>
  get<{ loungeId: string; messages: ChatRecord[] }>(
    `/api/v1/lounges/${loungeId}/chat/history?limit=${limit}`,
  );

export const requestSegue = (loungeId: string, body: Record<string, unknown>) =>
  post<SegueResponse>(`/api/v1/lounges/${loungeId}/ai/segue`, body);

/** §5.1's "join a Lounge via invite code". The code is what a Host shares; the id is what every
 *  other route is keyed by, and a listener is never handed one. */
export const lookupInvite = (code: string) =>
  get<{ loungeId: string; status: string }>(
    `/api/v1/lounges/by-invite/${encodeURIComponent(code.trim().toUpperCase())}`,
  );

/** §6.3's three dashboard reads. Each takes its subject from the JWT and nothing else: a
 *  `userId` query param would let any signed-in listener read someone else's taste profile, and
 *  the route already refuses one (#34's test sends it and gets its own dashboard back). */
export const readDashboard = () => get<Dashboard>('/api/v1/dashboard/me');

export const readRecommendations = () => get<Recommendations>('/api/v1/recommendations/me');

export const readAnalytics = (loungeId: string) =>
  get<SessionAnalytics>(`/api/v1/lounges/${encodeURIComponent(loungeId)}/analytics`);
