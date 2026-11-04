/** The catalog, fetched once and shared.

Frames identify records by id (§7.3.4), so every pane needs the same lookup: Now Playing shows a
title, Next Up shows durations, the stage shows candidate names, and the recommendation rail needs
the acoustic vector. Fetching it in each of them would be six requests for one table.

The load lives here rather than in the socket because the catalog is not room state — it does not
move with revisions, and a lounge that ends still has the same records in it.
*/
import { useEffect, useState } from 'react';

import { ApiError, listTracks, type Track } from '../api/rest';

export interface Catalog {
  byId: Map<string, Track>;
  tracks: Track[];
  error: string | null;
  loading: boolean;
}

export function useCatalog(): Catalog {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    listTracks()
      .then((result) => {
        if (alive) setTracks(result);
      })
      .catch((reason: unknown) => {
        if (!alive) return;
        setError(reason instanceof ApiError ? reason.message : 'the catalog did not load');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  return { tracks, byId: new Map(tracks.map((t) => [t.id, t])), error, loading };
}

/** A track id off a frame, rendered honestly whether or not the catalog knows it. */
export function labelOf(catalog: Catalog, trackId: string | null): string {
  if (trackId === null) return 'nothing queued';
  const track = catalog.byId.get(trackId);
  return track ? `${track.title} — ${track.artist}` : trackId;
}
