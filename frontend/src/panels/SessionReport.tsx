/** A session report — §6.3's five figures for one past Lounge, and the shape of its mood.

Every number here is added up from the append-only event log by `app/analytics/session.py`; this
panel's whole job is not to contradict that. So the rules it follows are the ones
`client/dashboardModel.ts` is tested against: a session that never opened has no length rather than
a length of zero, and a sentiment line drawn through one reading is a dot with a caveat, not a
trend.

The reconciliation this panel exists to make possible is §6.3's acceptance criterion — the figures
have to match the log for at least one manually verified session. That is why the source is named on
screen: a reader who can see which table a figure came from can check it.
*/
import { useCallback, type ReactNode } from 'react';

import { readAnalytics } from '../api/rest';
import { analyticsHeadline, sentimentTimeline } from '../client/dashboardModel';
import { useResource } from '../client/useResource';

const SPARK_WIDTH = 320;
const SPARK_HEIGHT = 60;

export interface SessionReportProps {
  loungeId: string;
  inviteCode: string;
  onBack: () => void;
}

export function SessionReport({ loungeId, inviteCode, onBack }: SessionReportProps) {
  const load = useCallback(() => readAnalytics(loungeId), [loungeId]);
  const { data, error, loading } = useResource(load);

  if (loading) return <Card title="Session report"><p className="empty">adding up the log…</p></Card>;
  if (error !== null)
    return (
      <Card title="Session report">
        <p className="error-strip">{error}</p>
        <button type="button" className="ghost" onClick={onBack}>
          back to the list
        </button>
      </Card>
    );
  if (data === null) return null;

  const timeline = sentimentTimeline(data.sentimentTimeline, SPARK_WIDTH, SPARK_HEIGHT);

  return (
    <Card title={`Session report · ${inviteCode}`}>
      <p className="sub">
        {analyticsHeadline(data)} · {data.status} · every figure counted from the <code>queue_events</code> log
      </p>

      <div className="figures">
        <span>peak {data.peakParticipants} seated</span>
        <span>{data.battlesHeld} battle{data.battlesHeld === 1 ? '' : 's'}</span>
        <span>{data.aiQuestionsAsked} DJ question{data.aiQuestionsAsked === 1 ? '' : 's'}</span>
      </div>

      <h3 className="chart-label">Sentiment through the session</h3>
      <svg
        className="spark"
        viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
        role="img"
        aria-label={timeline.renderable ? 'the session’s rolling sentiment, left to right in time' : 'one sentiment reading'}
      >
        <line x1="0" y1={SPARK_HEIGHT / 2} x2={SPARK_WIDTH} y2={SPARK_HEIGHT / 2} className="midline" />
        {timeline.renderable && <polyline points={timeline.points} className="trace" />}
        {timeline.dots.map((dot, index) => (
          <circle key={index} cx={dot.x} cy={dot.y} r="3" className="reading" />
        ))}
      </svg>
      <p className="sub">{timeline.note}</p>
      <p className="sub">
        each reading is the room’s rolling score, 0 at the bottom and 1 at the top; the dashed rule is neutral
      </p>

      <div className="row">
        <button type="button" className="ghost" onClick={onBack}>
          back to the list
        </button>
      </div>
    </Card>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
