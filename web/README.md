# Lana web app

The browser side of the demo: Home (the mic), Working (calls, live transcript, quotes) and Done (result and savings). It talks to the backend over `POST /cases` and the `/ws/case/{id}` WebSocket. No API keys live here.

## Run

```bash
cd web
npm install
cp .env.example .env     # Windows: copy .env.example .env
npm run dev              # http://localhost:5173
```

The backend must be running (`cd backend && uvicorn app.main:app`) with `WEB_ORIGIN=http://localhost:5173`.

## Build

```bash
npm run build            # type-checks, then builds to dist/
```

## Where things live

- `src/lib/player.ts`: the playback timeline. Plays every message in `seq` order, reveals each line's words while its audio plays, and never waits forever (failed audio reveals at 2.6 words a second, audio is capped at 15 s).
- `src/lib/audio.ts`: unlocks audio on the first click and plays each line's mp3.
- `src/lib/socket.ts`, `src/lib/api.ts`: server calls. `src/lib/types.ts` mirrors `backend/app/schemas/events.py`.
- `src/lib/state.ts`: turns events into what the screens show.
- `src/lib/clinics.ts`: public info for the demo clinics (names, distance, rating). No prices.
- The footer on Home has testing switches: voice on or off, and 2 or 3 clinics.
