# Deploying Lana

The backend (FastAPI, from `backend/`) runs on **Railway**. The web app (Vite, from `web/`) runs on
**Cloudflare Pages** as the project `lana-health`, at `https://lana-health.pages.dev`.

Order matters a little: deploy the backend first to get its domain, then the web app with that
domain baked in, then tell the backend the web app's origin.

## 1. Backend on Railway

1. **New project.** In Railway, choose New Project → Deploy from GitHub repo, and pick this repo.
2. **Root directory.** Open the service → Settings → Source → Root Directory: `backend`.
3. **Config file.** Settings → Config-as-code → Railway config file: `/backend/railway.json`.
   Railway does not apply the root directory to this path, so it needs the `/backend/` prefix. The file sets:
   - builder: Railpack (Python comes from `backend/.python-version`, 3.13; dependencies come from
     `backend/requirements.txt`, which installs the project from `pyproject.toml`)
   - start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - health check: `/health`

   If you skip the config file, set the start command and health check path by hand in Settings → Deploy.
4. **Variables.** Under Variables, add the values from your local `.env`. Railway reads no `.env` file.

   | Variable | Needed | Value |
   |---|---|---|
   | `META_API_KEY` | yes | your Meta key |
   | `META_BASE_URL` | yes | as in your `.env` |
   | `MODEL_FAST`, `MODEL_SMART`, `MODEL_STT` | yes | as in your `.env` |
   | `STT_REALTIME_URL` | yes | as in your `.env` |
   | `ELEVENLABS_API_KEY` | yes | your ElevenLabs key |
   | `VOICE_AGENT`, `VOICE_CLINIC_1`, `VOICE_CLINIC_2`, `VOICE_CLINIC_3` | yes | ElevenLabs voice IDs |
   | `VOICE_PATIENT` | recommended | the Watch mode patient's voice (blank uses `VOICE_CLINIC_1`) |
   | `WEB_ORIGIN` | yes | `https://lana-health.pages.dev` (set it in step 3 below; you can add it now) |
   | `WEB_ORIGIN_REGEX` | optional | `https://[a-z0-9-]+\.lana-health\.pages\.dev` to allow Pages preview deploys |
   | `CALL_REASONING`, `INTAKE_MODEL`, `INTAKE_REASONING` | optional | as in your `.env` |
   | `ELEVENLABS_MODEL*`, `ELEVENLABS_TAG_AGENT`, `ELEVENLABS_STABILITY`, `ELEVENLABS_SIMILARITY`, `ELEVENLABS_STYLE`, `ELEVENLABS_SPEAKER_BOOST`, `AGENT_NAME` | optional | as in your `.env` |
   | `END_GRACE_MS`, `JUDGE_SILENCE_S`, `JUDGE_HOLD_S`, `DEMO_CHECKLIST`, `TIMEZONE` | optional | as in your `.env` |
   | `MAX_RUNS_PER_CASE`, `MAX_CASES_PER_IP_HOUR` (default 20), `MAX_LLM_CALLS_PER_CASE`, `MAX_TTS_CHARS_PER_CASE`, `MAX_TTS_CHARS_PER_DAY`, `MAX_STT_SECONDS_PER_CASE` | optional | demo limits; blank uses the defaults in `.env.example` |

   Railway sets `PORT` itself. Don't add it.
5. **Domain.** Settings → Networking → Generate Domain. You get something like
   `https://lana-backend-production.up.railway.app`. Use it in step 2.
6. **Deploy and check.** Open `https://<railway domain>/health`:
   - `{"ok": true, "missing": []}` means it's ready.
   - `{"ok": false, "missing": ["ELEVENLABS_API_KEY"]}` names the keys still to set (names only, never values).
     The deploy stays up so you can see this. Add the key and Railway redeploys.

**About the TTS cache.** Railway's disk is temporary, so the cache of Lana's fixed lines is rebuilt
after every deploy. This runs in the background once the server is up, so startup never waits on
ElevenLabs. The logs show `TTS warm-up took …` and then `TTS prewarm took …`. The first visitor
right after a deploy may hear a line or two arrive a little slower.

## 2. Web on Cloudflare Pages

1. **New project.** In Cloudflare, go to Workers & Pages → Create → Pages → Connect to Git, and pick this repo.
   Name the project `lana-health`.
2. **Build settings.**
   - Framework preset: None (or Vite)
   - Root directory: `web`
   - Build command: `npm run build`
   - Build output directory: `dist`
3. **Environment variables** (Production, and Preview if you use previews):
   - `VITE_API_URL` = `https://<railway domain>`
   - `VITE_WS_URL` = `wss://<railway domain>` (optional: left blank, it is derived from `VITE_API_URL`)

   These are baked in at build time, so change them by redeploying the Pages project.
   A build without `VITE_API_URL` shows "This site isn't connected to Lana's server yet."
4. **Deploy.** The site comes up at `https://lana-health.pages.dev`. HTTPS is built in, which the
   browser needs before it allows the mic and audio.

No `_redirects` file is needed. The app has a single page and no client routes, and Pages already
serves `index.html` for unknown paths when there's no `404.html`.

## 3. Connect the two

On Railway, set `WEB_ORIGIN=https://lana-health.pages.dev`. Add more origins comma-separated if
needed, e.g. `https://lana-health.pages.dev,http://localhost:5173`. For preview deploys also set
`WEB_ORIGIN_REGEX=https://[a-z0-9-]+\.lana-health\.pages\.dev`. Railway redeploys on save.

## 4. Check it works

1. `curl https://<railway domain>/health` returns `{"ok":true,"missing":[]}`.
2. `curl https://<railway domain>/config/judge-sheet` returns Parkway's facts.
3. Open `https://lana-health.pages.dev`:
   - **Watch the demo**: the demo patient talks, Lana calls two clinics, and the callback rings.
   - **Talk to Lana**: the browser asks for the mic. Allow it, then speak.
   - **Be the clinic**: the script card shows Parkway's prices.
4. If the site says it can't reach the server, check that `VITE_API_URL` is right (then redeploy Pages)
   and that `WEB_ORIGIN` matches the site's exact origin, with no trailing slash. A CORS error in the
   browser console means `WEB_ORIGIN` is wrong. The case socket uses the same rules: a refused
   origin closes it with code 1008, and Railway logs `socket refused: origin … is not allowed`.
5. Railway logs show each case (`case=… created`), any limits hit, and server errors with their
   tracebacks. The browser only ever gets a generic "Something went wrong" message.
