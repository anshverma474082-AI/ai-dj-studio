/** One GET, with the three states a reader needs to see.

Three panels need this (the dashboard, the rail, and a session report) and each of them has to
distinguish "still loading" from "refused" from "empty", because those read differently on a screen:
an empty recommendation rail is a fact about the listener, a failed one is a fact about the server,
and showing either as the other is how a dashboard teaches someone to distrust it.

`load` is the caller's own `useCallback`. That is the seam: a panel that reads a different lounge
passes a different function and gets a new fetch, and one that does not keeps its result. It also
means an inline arrow here re-runs the read on every render, so callers hold theirs still.
*/
import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/rest';

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(load: () => Promise<T>): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    load()
      .then((result) => {
        if (alive) setData(result);
      })
      .catch((reason: unknown) => {
        if (!alive) return;
        setError(reason instanceof ApiError ? reason.message : 'the read did not answer');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [load, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  return { data, error, loading, reload };
}
