"""Reading a session back after it has ended — PRD §6.3's dashboard and analytics half.

`session.py` holds the arithmetic; the routes that call it live in `app/api/`. Nothing here owns
state: the numbers are derived from the append-only log on every read, which is what lets a
reviewer reconcile them against that log.
"""
