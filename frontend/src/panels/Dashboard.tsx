/** The home base §6.3 asks for: where you have been, what you listen like, and what to play next.

Three reads, none of which the socket could have answered — the dashboard is what a person sees when
they are *not* in a room, and §6.3's rail is trained on every room they have ever been in. The
panel keeps them separate so each can fail on its own: a dashboard whose recommendation rail 500'd
should still show the lounge list, and a reader who cannot tell which half broke tends to distrust
both.

The `gate` prop is a slot rather than a re-declaration. `Entry.tsx` already knows how an invite code
becomes a seat and how a seed track becomes a room; a second copy of that here would be two places
to get the join flow wrong.

The rail's refresh button is not a convenience. §6.3's acceptance criterion is that the rail moves
when new interaction data lands *without* a retrain-and-redeploy, and the only way to show that in a
recording is to be in a room, hear something, come back here, and watch the ranking change without
the server being restarted.
*/
import { useCallback, useState, type ReactNode } from 'react';

import { readDashboard, readRecommendations, type User } from '../api/rest';
import { railIsPersonal, railReason, railScores, splitLounges, tasteCopy, type LoungeRow } from '../client/dashboardModel';
import { useResource } from '../client/useResource';
import { SessionReport } from './SessionReport';

export interface DashboardProps {
  who: User;
  onEnter: (loungeId: string) => void;
  onSignOut: () => void;
  gate: ReactNode;
}

export function Dashboard({ who, onEnter, onSignOut, gate }: DashboardProps) {
  const loadDashboard = useCallback(() => readDashboard(), []);
  const loadRails = useCallback(() => readRecommendations(), []);
  const room = useResource(loadDashboard);
  const rail = useResource(loadRails);
  const [reportId, setReportId] = useState<string | null>(null);

  const dashboard = room.data;
  const strategy = rail.data?.strategy ?? 'hybrid';
  const personalRail = railIsPersonal(strategy);
  const split = splitLounges(dashboard?.lounges ?? []);
  const taste = dashboard === null ? null : tasteCopy(dashboard.taste);
  const chosen = split.past.find((row) => row.loungeId === reportId) ?? null;

  return (
    <main className="console">
      <header className="status-bar">
        <strong className="lounge">AI DJ Studio</strong>
        <span className="meta">
          {room.loading ? 'reading your history…' : `${dashboard?.lounges.length ?? 0} lounge${(dashboard?.lounges.length ?? 0) === 1 ? '' : 's'} on record`}
        </span>
        <span className="who">
          {who.displayName}
          <em> {who.role}</em>
        </span>
        <button type="button" className="ghost" onClick={room.reload}>
          reload
        </button>
        <button type="button" className="ghost" onClick={onSignOut}>
          sign out
        </button>
      </header>

      <div className="banner-stack">
        {room.error !== null && <p className="error-strip">the dashboard did not answer: {room.error}</p>}
        {rail.error !== null && <p className="error-strip">the rail did not answer: {rail.error}</p>}
      </div>

      <div className="grid">
        <div className="column">
          <section className="card" aria-label="My lounges">
            <h2>My lounges</h2>
            {split.open.length === 0 && split.past.length === 0 && (
              <p className="empty">{room.loading ? 'loading…' : 'no lounge has taken you in yet'}</p>
            )}

            {split.open.map((row) => (
              <LoungeRow key={row.loungeId} row={row} onEnter={onEnter} onReport={null} />
            ))}
            {split.past.map((row) => (
              <LoungeRow
                key={row.loungeId}
                row={row}
                onEnter={onEnter}
                onReport={() => setReportId(row.loungeId)}
              />
            ))}
            {split.past.length > 0 && (
              <p className="sub">a room that has ended is not rejoinable — its report is, and it comes from the log.</p>
            )}
          </section>

          {gate}
        </div>

        <div className="column">
          <section className="card" aria-label="Acoustic taste">
            <h2>Acoustic taste</h2>
            {taste === null ? (
              <p className="empty">{room.loading ? 'loading…' : 'nothing to read yet'}</p>
            ) : (
              <>
                <p className="sub">
                  {taste.hasHistory
                    ? `${dashboard?.taste.records} record${dashboard?.taste.records === 1 ? '' : 's'} on record`
                    : 'nothing on record yet — the profile fills from rooms you have actually sat in'}
                </p>
                <div className="figures">
                  <span>{taste.bpm}</span>
                  <span>{taste.energy}</span>
                </div>
                <ol className="keys">
                  {taste.keys.map((entry) => (
                    <li key={entry.key}>
                      <span className="key">{entry.key}</span>
                      <span className="meter">
                        <span className="fill" style={{ width: `${Math.round(entry.share * 100)}%` }} />
                      </span>
                      <span className="count">
                        {entry.records} record{entry.records === 1 ? '' : 's'}
                      </span>
                    </li>
                  ))}
                </ol>
                {taste.hasHistory && taste.keys.length === 0 && <p className="empty">no keys on record</p>}
              </>
            )}
          </section>

          <section className="card" aria-label="Recommended for you">
            <h2>Recommended for you</h2>
            <div className="row">
              {rail.data !== null && <span className="tag">{rail.data.strategy}</span>}
              <button type="button" className="ghost" onClick={rail.reload} disabled={rail.loading}>
                {rail.loading ? 'ranking…' : 're-rank'}
              </button>
            </div>
            {rail.data === null && rail.error === null && <p className="empty">the rail is loading…</p>}
            {rail.data !== null && rail.data.items.length === 0 && (
              <p className="empty">the catalog has nothing left that this rail can rank</p>
            )}
            <ol className="rail">
              {rail.data?.items.map((item, index) => (
                <li key={item.trackId} className="rail-row">
                  <span className="pos">{index + 1}</span>
                  <span className="who">
                    <strong>{item.title}</strong>
                    <em>{item.artist}</em>
                    <em className="why">{railReason(item, strategy)}</em>
                  </span>
                  <span className="votes" title="content score · collaborative score">
                    {railScores(item, strategy) ?? '—'}
                  </span>
                </li>
              ))}
            </ol>
            {rail.data !== null && rail.data.items.length > 0 && (
              <p className="sub">
                {personalRail &&
                  'two numbers per record: how close it is to what you have heard, and how the room ranked it. '}                Nothing here was retrained to produce them.
              </p>
            )}
          </section>
        </div>

        <div className="column">
          {chosen === null ? (
            <section className="card" aria-label="Session report">
              <h2>Session report</h2>
              <p className="empty">pick a lounge from the list to see how long it ran, how many sat in, how many battles it held and where the mood went.</p>
            </section>
          ) : (
            <SessionReport
              loungeId={chosen.loungeId}
              inviteCode={chosen.inviteCode}
              onBack={() => setReportId(null)}
            />
          )}
        </div>
      </div>
    </main>
  );
}

function LoungeRow({
  row,
  onEnter,
  onReport,
}: {
  row: LoungeRow;
  onEnter: (loungeId: string) => void;
  onReport: (() => void) | null;
}) {
  return (
    <div className="lounge-row">
      <span className="who">
        <strong>{row.inviteCode}</strong>
        <em>
          {row.role} · {row.status}
          {row.endedAt !== null && ` · ended ${row.endedAt.slice(0, 10)}`}
        </em>
      </span>
      <span className="row-actions">
        {row.canRejoin && (
          <button type="button" onClick={() => onEnter(row.loungeId)}>
            rejoin
          </button>
        )}
        {onReport !== null && (
          <button type="button" className="ghost" onClick={onReport}>
            session report
          </button>
        )}
      </span>
    </div>
  );
}
