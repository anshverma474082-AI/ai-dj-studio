# Track A walkthrough recording

This is a short capture checklist for the final submission recording. It does not claim a
recording has already been made. Keep the project and browser on the same local machine so
the test reflects the documented scope.

## Prepare

1. Complete the README setup once, including PostgreSQL, `.env`, migrations, seed data, and
   generated audio (`make media`).
2. Start the app with `make dev` and open `http://localhost:5174` in two browser windows.
3. Sign in as Host in the first window and Listener in the second. Demo credentials are
   listed in the README. Join the seeded lounge with invite code `SP-DEMO`.
4. Start the operating-system screen recorder and include both windows in the recording.

## Record one continuous run

1. Show the same Now Playing and Next Up state in both windows.
2. Cast a queue vote from the Listener window; show both clients update.
3. Send a clearly positive chat burst, then a clearly negative burst; show the sentiment
   indicator move.
4. Submit at least two different tracks from the Listener window. In the Host window, open
   a timed Soundclash over those submissions. Vote from the Listener window and show the
   result and queue splice in both windows.
5. Request or approve a segue when the current track is inside its outro window. Show the
   selected track, BPM/Camelot reason, generated line, and metadata source citations.
6. Ask a track-lore question whose answer is in the authored metadata, then one the metadata
   cannot answer. Show the citations and the graceful decline.
7. Briefly show the dashboard/recommendation rail, session analytics, and Host controls.

Save the recording under `evidence/` or attach it separately to the submission. Include a
short README note with the date, test accounts/roles used, and any step that required a
manual action. Do not edit the recorded results after capture.
