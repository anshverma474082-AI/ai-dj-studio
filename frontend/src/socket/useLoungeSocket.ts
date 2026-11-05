/** One socket per lounge, kept alive through the network's ordinary blips (§10.3).
 *
 * Three rules this hook exists to hold:
 *
 * **The server's snapshot is the only baseline.** A reconnect gets one automatically — the join
 * path sends it before anything else — so the client never has to guess what it missed while it
 * was down. The one exception is a frame that arrives with a revision it cannot account for; then
 * `applyFrame` says `resync` and this file asks for a fresh snapshot rather than rendering an
 * order it cannot prove.
 *
 * **The token is in the query string because a browser cannot set a header on an upgrade** (§7.3.4
 * notes the same). That is why it is the short-lived access token, why it is never logged here, and
 * why nothing sensitive is put in a socket URL by hand.
 *
 * **A refused socket stays refused.** 1008 is the server's "policy violation" close: an unheld
 * token, a locked room, a removal. Auto-reconnecting there would be a client hammering a door the
 * room has deliberately shut, so the close reason is shown and the retry stops.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { accessToken as currentAccessToken } from '../api/rest';
import {
  applyFrame,
  initialState,
  loadHistory,
  type Frame,
  type HistoryRecord,
  type LoungeState,
} from '../state/loungeReducer';

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed';

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 5000;
const PING_EVERY_MS = 5000;

/** 1008 = policy violation, and the only close that should not be retried. */
const POLICY_CLOSE = 1008;

export function socketUrl(loungeId: string, token: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.host}/ws/lounges/${encodeURIComponent(loungeId)}?token=${encodeURIComponent(token)}`;
}

export interface LoungeSocket {
  state: LoungeState;
  status: SocketStatus;
  notice: string | null;
  send: (event: string, payload: Record<string, unknown>) => boolean;
  requestSnapshot: () => void;
  /** Fold §5.1's persisted transcript in without disturbing anything the socket already said. */
  hydrate: (records: HistoryRecord[]) => void;
}

export function useLoungeSocket(loungeId: string | null): LoungeSocket {
  const [state, setState] = useState<LoungeState>(initialState);
  const [status, setStatus] = useState<SocketStatus>('idle');
  const [notice, setNotice] = useState<string | null>(null);

  const stateRef = useRef(state);
  const socketRef = useRef<WebSocket | null>(null);
  const loungeIdRef = useRef<string>('');
  if (loungeId !== null) loungeIdRef.current = loungeId;
  const closedByUs = useRef(false);
  const backoff = useRef(RECONNECT_BASE_MS);
  const timer = useRef<number | null>(null);

  const sendRaw = useCallback((event: string, payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify({ event, loungeId: loungeIdRef.current, payload, ts: Date.now() }));
    return true;
  }, []);

  const write = useCallback(
    (frame: Frame) => {
      const next = applyFrame(stateRef.current, frame);
      stateRef.current = next.state;
      setState(next.state);
      if (next.resync) sendRaw('lounge.snapshot.request', {});
    },
    [sendRaw],
  );

  useEffect(() => {
    // A new room starts from nothing. Without this the panels keep showing the room you just left
    // until this one's snapshot lands, and if that handshake is refused — locked, removed, an
    // expired token — the previous room stays on screen looking current. The dashboard makes
    // leaving and entering an ordinary move rather than a sign-out, so that window is now walkable.
    stateRef.current = initialState;
    setState(initialState);
    if (loungeId === null) return;

    let alive = true;
    let ping: number | null = null;

    const connect = () => {
      if (!alive) return;
      const token = currentAccessToken();
      if (!token) {
        setNotice('the session has no access token — sign in again');
        setStatus('closed');
        return;
      }
      setStatus('connecting');
      const socket = new WebSocket(socketUrl(loungeId, token));
      socketRef.current = socket;

      socket.onopen = () => {
        backoff.current = RECONNECT_BASE_MS;
        setNotice(null);
        setStatus('open');
        ping = window.setInterval(() => {
          sendRawAt(socket, loungeId, 'lounge.sync.ping', { clientSentAt: Date.now() });
        }, PING_EVERY_MS);
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        let frame: Frame;
        try {
          frame = JSON.parse(event.data) as Frame;
        } catch {
          // A frame this client cannot read is a bug on one side of the wire, and guessing at
          // the bytes is how a partial state reaches the screen. Say so, and ask for the truth.
          setNotice('a frame arrived that this client cannot parse');
          sendRawAt(socket, loungeId, 'lounge.snapshot.request', {});
          return;
        }
        if (alive) write(frame);
      };

      socket.onclose = (event: CloseEvent) => {
        if (ping !== null) window.clearInterval(ping);
        if (!alive || closedByUs.current) return;
        if (event.code === POLICY_CLOSE) {
          setStatus('closed');
          setNotice(event.reason || 'the room refused this connection');
          return;
        }
        setNotice('the socket dropped; reconnecting');
        setStatus('connecting');
        timer.current = window.setTimeout(connect, backoff.current);
        backoff.current = Math.min(backoff.current * 2, RECONNECT_MAX_MS);
      };

      socket.onerror = () => {
        // Starlette's close frame follows an error, and `onclose` is where the retry decision
        // lives. Nothing here, so an error cannot leave a spinner up forever.
      };
    };

    connect();

    return () => {
      alive = false;
      closedByUs.current = false;
      if (timer.current !== null) window.clearTimeout(timer.current);
      if (ping !== null) window.clearInterval(ping);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        closedByUs.current = true;
        socket.close();
      }
    };
    // A new lounge is a new room: the old one's state belongs to the old socket.
  }, [loungeId, write]);

  const send = useCallback(
    (event: string, payload: Record<string, unknown>) => sendRaw(event, payload),
    [sendRaw],
  );

  const requestSnapshot = useCallback(() => sendRaw('lounge.snapshot.request', {}), [sendRaw]);

  const hydrate = useCallback((records: HistoryRecord[]) => {
    const next = loadHistory(stateRef.current, records);
    stateRef.current = next;
    setState(next);
  }, []);

  return { state, status, notice, send, requestSnapshot, hydrate };
}

function sendRawAt(socket: WebSocket, loungeId: string, event: string, payload: Record<string, unknown>): void {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ event, loungeId, payload, ts: Date.now() }));
  }
}
