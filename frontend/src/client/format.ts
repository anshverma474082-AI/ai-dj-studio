/** Small shared formatting for the console's panels.

Kept in one file because the same three renderings appear in Now Playing, Next Up, the stage and
the banter feed, and a duration written two ways on one screen reads as two different numbers.
*/

/** `224` -> `3:44`. Seconds are the unit the catalog and every frame carry. */
export function clockTotal(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/** `tsSeconds` off a chat frame: the playhead it was spoken at, not the wall clock. */
export function clockPosition(tsSeconds: number | null): string {
  return tsSeconds === null ? '—' : clockTotal(tsSeconds);
}

/** §6.2's energy/valence are 0..1 floats; a bar reads faster than a third decimal place. */
export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** A battle deadline is epoch milliseconds (`open_frame` publishes `closesAt` that way). */
export function countdown(closesAt: number, nowMs: number): string {
  const remaining = Math.max(0, Math.round((closesAt - nowMs) / 1000));
  return remaining > 0 ? `${remaining}s left` : 'closing';
}
