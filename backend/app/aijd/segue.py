"""When the AI DJ should be making a segue decision — §6.2.

§6.2 scopes the decision to "as the current track approaches its configured outro
window", so this is a gate the endpoint enforces rather than a hint: answering
earlier would let a client collect a decision it has no business acting on yet.

Pure arithmetic, unit-tested without a database or a socket (§9.3).
"""

from __future__ import annotations


def is_in_outro_window(current_ts: int, duration_seconds: int, window_seconds: int) -> bool:
    """True when `current_ts` is inside the final `window_seconds` of the track.

    The boundary is inclusive and the comparison is a floor, not an equality: a
    client polling at 1 Hz will land either side of the opening second, and a track
    shorter than the window is inside it from the first tick rather than never.
    """
    return current_ts >= duration_seconds - window_seconds
