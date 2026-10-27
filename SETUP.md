# Patient Agent: Meta Hackathon Setup

> How to use this file: put this `SETUP.md` in the root of your `patient-agent` folder (replace the old one), open the folder in VS Code, open Claude Code in the terminal, and paste the prompt you were given. Claude Code will only follow the "Instructions for Claude Code" section.

This is the **hackathon build** (Meta Global AI Developer Hackathon, Oct 26 to Nov 4, 2026, Startup track). It is built fast with AI help. After the hackathon, the product is rebuilt from scratch by hand with a separate setup file.

---

## Instructions for Claude Code

Your only job is to make the folder match the tree in section 2, with empty files.

1. Create every folder and file in the tree in section 2 that does not exist yet.
2. **Every new file must be completely empty (0 bytes).** This includes `.py`, `.ts`, `.tsx`, `.md`, `.json`, `.toml`, `.css`, `.html`, `.gitignore`, `.env.example`, `CLAUDE.md` and `README.md`.
3. Do **not** write any code, comments, imports, config or text into any file.
4. Do **not** install packages, create a virtual environment, run git commands, run tests or start servers.
5. Do **not** create anything that is not in the tree.
6. **Files that already exist and are in the tree:** leave them exactly as they are, even if they contain something.
7. **Files that already exist but are NOT in the tree** (left over from an older setup):
   - If the file is 0 bytes, delete it.
   - If the file has any content, do not touch it. List it in your report.
   - After that, delete any folder that is now completely empty and is not in the tree.
   - Never touch `.git/`, `.venv/`, `node_modules/`, `.claude/`, or `SETUP.md`.
8. Never modify, empty or overwrite `SETUP.md`.
9. When done, print the final tree, then three lists: files created, files deleted, and leftover files you did not touch (with their size). Then stop.

Everything below this line is the builder's guide. Do not act on it unless asked later.

---

## 1. What we are building

A voice AI patient agent for cash-pay orthodontics in Dallas-Fort Worth. The patient talks to it for about 30 seconds. The agent then calls clinics, gets all-in quotes, negotiates the price down, books the best consult, and calls the patient back with the result and the savings.

**One-line pitch:** Hi, I'm Lana, your personal health companion. I'll handle your care, start to finish.

**Home screen copy (approved):**
- Headline: "Hi, I'm Lana, your personal health companion." / lime italic line: "I'll handle your care, start to finish."
- Under it: "I find the best clinics, call them, push the price down, and book the one that fits you. Just talk to me for 30 seconds."

### Meet Lana
- The agent is named **Lana**. She is the one voice the patient talks to and the one who calls the clinics.
- **Female voice**, warm, calm and confident, never pushy. The same voice everywhere (intake, calls, callback). Set it with `VOICE_AGENT`.
- She introduces herself by name: to the patient, "Hey, I'm Lana, your personal health companion! What do you need help with?" and to clinics, "Hi, this is Lana, an AI assistant calling for a patient, and this call is recorded."
- The patient can talk to her by name ("Lana, I want to get braces"). Her name never hides that she is an AI.
- In the app her lines are labeled **LANA**, not "Agent".

### The demo has three modes

1. **Watch it work:** the agent calls three AI demo clinics. Judges hear both voices and see a live transcript, the hold, the negotiation, and the price dropping.
2. **Be the clinic:** a judge answers one of the calls through their browser mic. A script card shows the clinic's prices, but the judge can go off script.
3. **Real phone call (stretch goal):** the agent calls a phone number that is on an allow list (the founder's own phone), to prove it works on real phone lines.

### The flow (same as the approved prototype)

1. **Voice intake:** a big mic orb. The agent asks up to 6 one-sentence questions out loud: treatment, location, budget, how they'll pay (monthly, pay in full if cheaper, HSA/FSA), x-rays or a treatment plan, and must-haves (for example evening hours). Tap chips are always shown as a fallback. If the patient answers several things at once, the agent skips those questions.
2. **Optional plan photo:** the patient can upload a photo of a treatment plan or x-ray summary. Muse Wonder reads it and shows "Is this right?"
3. **Research:** the agent picks 3 clinics from the demo clinic directory and shows a live agent log.
4. **Calls:** the agent calls the clinics one by one. Every call starts with the AI disclosure and the recording notice. It asks the clinic checklist (all-in price, records fee, what's included, monthly plan, down payment, next consult slot).
5. **Hold and resume:** when the first clinic is the best fit but not the cheapest, the agent asks it to hold, calls the others, then comes back.
6. **Negotiation:** with real competing quotes as leverage, the agent asks the best-fit clinic to match the lowest quote, waive the records fee, give a pay-in-full discount, and include extra retainers. The clinic can say no.
7. **Quotes panel:** live quote cards with prices struck through as they drop, a BEST badge, and a "Saved $X" counter.
8. **Close and wait:** the patient can hide everything ("You're free. I'll call you when it's done.").
9. **Callback:** a browser notification, then an incoming-call screen. The agent speaks the summary and asks "Want me to book it?" The patient says or taps yes.
10. **Done:** booked slot, total saved, and a line-by-line savings breakdown with the evidence for each number.

### Core design decisions (do not change without asking)

- **One agent to the user, a fixed pipeline inside.** The orchestrator owns the case and moves it through set states. AI is used only inside steps that need language or judgment.
- **Plain Python decides:** ranking, which clinic to negotiate with, call order, when to hold and resume. The AI never picks the winner or changes the order.
- **Every AI output that becomes data is strict JSON**, validated with Pydantic. Invalid output is retried once, then the step fails visibly.
- **Every fact has a source.** Each checklist answer stores the speaker, the exact words, and the second in the call where it was said.
- **Unknown stays unknown.** Missing answers are `None` and shown as "Not confirmed".
- **Honest negotiation.** The agent only uses real quotes it actually received as leverage. It never invents a competitor, a price or a reason. It never commits the patient to anything outside their preferences (for example it only offers pay in full if the patient said yes to that).
- **No medical judgment.** The agent never says whether treatment is needed or how severe a case is.
- **Always disclose the AI.** Every call opens with: "Hi, this is Lana, an AI assistant calling for a patient, and this call is recorded."
- **Demo clinics are real simulations, not scripts.** Each demo clinic is Muse Spark playing a receptionist with a hidden price sheet, a lowest acceptable price, and rules for what it will concede. The agent never sees these. Outcomes can differ between runs.
- **No real clinics during the hackathon.** Real calls go only to numbers on `ALLOWED_CALL_NUMBERS`, and only when `ENABLE_REAL_CALLS=true`.
- **No real patient data.** Use fixtures. The only real record allowed is the founder's own.
- **It must survive a live judge.** Every voice step has a fallback: typed input if the mic fails, text on screen if a voice fails, and a clear error message instead of a frozen screen.

### Which Meta model does what

| Job | Model | Why |
|---|---|---|
| Live call turns (agent and demo receptionists), intake questions | `MODEL_FAST` = Muse Spark 1.3 | Fast and cheap for many short turns |
| Reading a plan photo, choosing negotiation lines, final summary | `MODEL_SMART` = Muse Wonder | Multimodal and strongest reasoning |
| Speech to text for the patient, the judge, and phone audio | `MODEL_STT` = Muse Voice Transcribe | Streaming, detects when someone stopped talking |

Model names and endpoints come from environment variables, never hard-coded. On Oct 26, confirm the exact model IDs and endpoints in the Meta Model API docs and log them in `docs/decisions.md`.

### Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, WebSockets, Uvicorn |
| AI calls | Meta Model API through the `openai` Python SDK (OpenAI-compatible format, custom base URL) |
| Speech to text | Muse Voice Transcribe streaming endpoint |
| Text to speech | ElevenLabs (streaming), one voice per speaker. Meta has no text-to-speech model. |
| Validation | Pydantic v2 |
| Storage | SQLite through SQLAlchemy 2 (cases, transcripts, quotes) |
| Terminal testing | Typer CLI |
| Real phone call (stretch) | Twilio Voice with Media Streams |
| Frontend | React + TypeScript + Vite, Tailwind CSS, the lime-on-dark design from the prototype (Instrument Serif, Geist, Geist Mono) |
| Hosting | Railway (backend), Cloudflare Pages (frontend), ngrok while developing |
| Tests | pytest, pytest-asyncio |

---

## 2. Folder structure

Comments after `#` explain each file; they are not part of the file.

```
patient-agent/
├── SETUP.md                      # this file
├── CLAUDE.md                     # rules for Claude Code every session (contents in section 6)
├── README.md                     # how to install, run and demo
├── .gitignore
├── .env.example
├── docs/
│   ├── decisions.md              # design decisions log, newest first
│   ├── demo-script.md            # the exact demo run and the video script
│   └── submission.md             # answers for the hackathon submission form
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py             # Settings from .env, ranking weights, feature switches
│   │   ├── main.py               # FastAPI app: REST routes + the /ws/case/{id} WebSocket
│   │   ├── cli.py                # Typer commands to test each piece in the terminal
│   │   │
│   │   ├── schemas/              # Pydantic data shapes
│   │   │   ├── __init__.py
│   │   │   ├── enums.py          # CaseStatus, CallStatus, Speaker, DemoMode
│   │   │   ├── preferences.py    # Preferences (the 6 intake answers)
│   │   │   ├── plan.py           # ProcedureItem, ParsedPlan (from a plan photo)
│   │   │   ├── clinic.py         # Clinic (public info) and PriceSheet (hidden, demo clinics only)
│   │   │   ├── checklist.py      # SourcedAnswer, ClinicChecklist
│   │   │   ├── quote.py          # Quote (built from a filled checklist)
│   │   │   ├── negotiation.py    # Concession, NegotiationResult
│   │   │   ├── offer.py          # Offer (ranked result with savings)
│   │   │   ├── events.py         # every WebSocket event sent to the browser
│   │   │   └── case.py           # Case (ties everything together)
│   │   │
│   │   ├── agent/                # the patient's agent
│   │   │   ├── __init__.py
│   │   │   ├── intake.py         # asks up to 6 questions, fills Preferences from speech
│   │   │   ├── plan_reader.py    # plan photo -> ParsedPlan (Muse Wonder vision)
│   │   │   ├── researcher.py     # picks 3 clinics from the demo directory, writes the agent log
│   │   │   ├── caller.py         # runs one call: disclosure, checklist questions, turn loop
│   │   │   ├── extractor.py      # transcript -> ClinicChecklist + Quote, with evidence
│   │   │   ├── negotiator.py     # chooses the target and moves (Python), phrases them (AI)
│   │   │   ├── ranker.py         # plain Python ranking -> top 3 Offers
│   │   │   └── orchestrator.py   # state machine for the whole case, emits events
│   │   │
│   │   ├── clinics/              # the other side of the call
│   │   │   ├── __init__.py
│   │   │   ├── simulator.py      # demo receptionist: Muse Spark + hidden PriceSheet + concession rules
│   │   │   └── judge_line.py     # routes a judge's mic audio in as the clinic side
│   │   │
│   │   ├── voice/
│   │   │   ├── __init__.py
│   │   │   ├── stt.py            # Muse Voice Transcribe streaming (patient, judge, phone)
│   │   │   ├── tts.py            # ElevenLabs streaming, picks the voice per speaker
│   │   │   └── audio_bus.py      # sends both sides of a call to the browser so judges hear it
│   │   │
│   │   ├── integrations/
│   │   │   ├── __init__.py
│   │   │   ├── meta_llm.py       # call_json() and stream_chat() over the Meta Model API
│   │   │   └── twilio_call.py    # stretch: real phone call with Media Streams, allow list only
│   │   │
│   │   ├── prompts/              # prompts live in files, never inside Python strings
│   │   │   ├── intake.md
│   │   │   ├── plan_reader.md
│   │   │   ├── caller.md
│   │   │   ├── extractor.md
│   │   │   ├── negotiator.md
│   │   │   ├── receptionist.md
│   │   │   └── summary.md
│   │   │
│   │   ├── data/
│   │   │   └── demo_clinics.json # 3 fake clinics: public info, voice, hidden price sheet, floor, concessions, slots
│   │   │
│   │   └── db/
│   │       ├── __init__.py
│   │       └── store.py          # SQLite tables and save/load helpers
│   │
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── fixtures/
│       │   ├── transcripts/      # fake call transcripts (with the AI disclosure)
│       │   └── plans/            # fake treatment plan text and images
│       ├── test_ranker.py
│       ├── test_negotiator.py
│       ├── test_extractor.py
│       ├── test_simulator.py
│       └── test_orchestrator.py
└── web/
    ├── README.md
    ├── .env.example
    ├── package.json
    ├── index.html
    ├── vite.config.ts
    ├── tsconfig.json
    └── src/
        ├── main.tsx
        ├── App.tsx               # switches between screens by case status
        ├── styles.css            # design tokens and animations from the prototype
        ├── lib/
        │   ├── socket.ts         # WebSocket client and event types
        │   └── audio.ts          # mic capture, playback queue, autoplay unlock
        ├── screens/
        │   ├── Intake.tsx        # mic orb, spoken questions, tap chips
        │   ├── Mission.tsx       # agent log, live call, quotes panel
        │   ├── Callback.tsx      # notification, incoming call, booking yes/no
        │   └── Done.tsx          # booked, stats, savings breakdown
        └── components/
            ├── MicOrb.tsx
            ├── Transcript.tsx
            ├── ClinicStatus.tsx
            ├── QuoteCard.tsx
            ├── SavingsBreakdown.tsx
            ├── JudgeScriptCard.tsx
            └── Toast.tsx
```

### Config file contents (fill these in yourself)

**`.gitignore`**
`.env`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `*.db`, `data/*.db`, `uploads/`, `recordings/`, `.DS_Store`, `node_modules/`, `dist/`, `web/.env`

**`.env.example`** (backend)
```
META_API_KEY=
META_BASE_URL=https://api.meta.ai/v1
MODEL_FAST=muse-spark-1.3
MODEL_SMART=muse-wonder-1.0
MODEL_STT=muse-voice-transcribe-1.0
STT_REALTIME_URL=wss://api.meta.ai/v1/asr/realtime

ELEVENLABS_API_KEY=
AGENT_NAME=Lana
VOICE_AGENT=          # Lana's voice: a female ElevenLabs voice ID
VOICE_CLINIC_1=
VOICE_CLINIC_2=
VOICE_CLINIC_3=

DATABASE_URL=sqlite:///./data/dev.db
TIMEZONE=America/Chicago
WEB_ORIGIN=http://localhost:5173
PUBLIC_BASE_URL=

# Safety switches: keep false until you mean it
ENABLE_REAL_CALLS=false
ALLOWED_CALL_NUMBERS=

# Stretch goal only
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=
```

**`web/.env.example`**
```
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000
```

**`backend/pyproject.toml`**
Python `>=3.12`. Dependencies: `fastapi`, `uvicorn[standard]`, `websockets`, `pydantic>=2`, `pydantic-settings`, `openai`, `httpx`, `python-dotenv`, `python-multipart`, `sqlalchemy>=2`, `typer`, `rich`, `elevenlabs`, `twilio`. Dev: `pytest`, `pytest-asyncio`, `pytest-mock`. Script entry: `agent = "app.cli:app"`.

**`web/package.json`**
`react`, `react-dom`, `typescript`, `vite`, `@vitejs/plugin-react`, `tailwindcss`, `@tailwindcss/vite`. Scripts: `dev`, `build`, `preview`.

---

## 3. What goes in each piece

### 3.1 Schemas
- **`Preferences`:** `treatment` ("braces", "aligners", "not sure"), `zip_code`, `max_distance_mi`, `budget_max: int`, `pay_in_full_ok: bool`, `uses_hsa_fsa: bool`, `prefers_monthly: bool`, `has_xrays: bool`, `has_plan: bool`, `must_haves: list[str]` (for example "evening hours"), `free_times: list[str]`.
- **`Clinic`** (what the agent may see): `id`, `name`, `distance_mi`, `rating`, `review_count`, `hours`, `review_summary`, `phone`, `is_demo: bool`, `voice_id`.
- **`PriceSheet`** (hidden, only `simulator.py` reads it): `list_price`, `records_fee`, `consult_fee`, `monthly`, `down_payment`, `floor_price`, `will_price_match: bool`, `will_waive_records: bool`, `pay_in_full_discount_pct`, `will_add_retainers: bool`, `open_slots`, `personality` (one line, for example "friendly but checks with a manager before discounts").
- **`SourcedAnswer`:** `value`, `speaker`, `evidence` (exact words), `call_id`, `t_seconds`.
- **`ClinicChecklist`:** every field is `SourcedAnswer | None`: `total_price`, `records_fee`, `consult_fee`, `includes`, `monthly_payment`, `down_payment`, `payment_plan`, `next_slots`, `evening_hours`, `needs_exam_first`.
- **`Quote`:** `clinic_id`, `checklist`, `all_in_price` (price plus fees that are not included), `missing_fields`, `is_solid`.
- **`Concession`:** `label`, `amount: int` (0 for non-cash items like retainers), `evidence: SourcedAnswer`.
- **`NegotiationResult`:** `clinic_id`, `first_all_in`, `final_all_in`, `concessions`, `refused: list[str]`.
- **`Offer`:** `rank`, `clinic`, `quote`, `negotiation | None`, `score`, `tradeoffs`, `unknowns`.
- **`events.py`:** one Pydantic model per event the browser gets: `agent_said`, `user_said`, `log_line`, `call_status` (dialing, live, on_hold, negotiating, ended), `transcript_partial`, `transcript_final`, `quote_updated`, `concession_won`, `offers_ready`, `callback_started`, `booked`, `error`.
- **Money is whole dollars as `int`. Times are timezone-aware in `TIMEZONE`.**

### 3.2 `integrations/meta_llm.py`
- `call_json(prompt, schema, model, images=None)`: returns a validated Pydantic object. Includes the schema in the prompt, retries once with the validation error, then raises.
- `stream_chat(messages, model)`: streams text tokens for live call turns, so text-to-speech can start before the full reply is done.
- Every request uses Standard mode (no training on our data).

### 3.3 Voice (`voice/`)
- `stt.py`: opens the streaming connection, yields partial and final transcripts, and signals when the speaker has stopped (end of turn).
- `tts.py`: streams audio for a line of text with the voice for that speaker. If it fails, the line still appears on screen and the call continues.
- `audio_bus.py`: sends both sides of the call (agent voice and clinic voice) to the browser, so judges hear the whole conversation.
- Target: the agent starts replying within about 1.5 seconds after the other side stops talking.

### 3.4 `clinics/simulator.py`
- One function per turn: given the conversation so far and the clinic's `PriceSheet`, return the receptionist's next line using `MODEL_FAST` and `prompts/receptionist.md`.
- The receptionist answers only from its price sheet, never goes below `floor_price`, only concedes what its rules allow, sometimes "checks with a manager", and can say no.
- `judge_line.py` replaces the simulator for one clinic when a judge plays it: the judge's mic goes through `stt.py`, and the browser shows `JudgeScriptCard` with that clinic's price sheet.

### 3.5 `agent/caller.py` and `agent/extractor.py`
- The caller opens with the disclosure, then works through the checklist in natural order, one question at a time, and ends politely.
- After each clinic turn, the extractor updates the `ClinicChecklist` with evidence and timestamps, and emits `quote_updated`.
- A call should last about 30 to 40 seconds.

### 3.6 `agent/negotiator.py`
- **Target (plain Python):** the clinic with the best score on everything except price (fit with must-haves, rating, distance, slots), when it is not already the cheapest solid quote.
- **Moves (plain Python list, in order):** match the lowest real all-in quote, waive the records fee, pay-in-full discount (only if `pay_in_full_ok`), add a second set of retainers (only ask, never insist). Stop after a clear no, or after 4 asks.
- **Phrasing (AI):** `MODEL_SMART` with `prompts/negotiator.md` turns each move into one polite sentence that cites the real competing quote.
- Every concession is stored with its evidence. Total saved = `first_all_in - final_all_in`.

### 3.7 `agent/ranker.py` (plain Python)
- Hard filters: within budget (after negotiation), within distance, payment plan if `prefers_monthly`.
- `score = 0.40*price + 0.20*fit + 0.15*rating + 0.15*distance + 0.10*availability`, all sub-scores 0 to 1, missing data scores 0.5. Weights live in `config.py`.
- Returns up to 3 Offers. Then `MODEL_FAST` writes 1 to 2 tradeoff lines each. The AI never changes the order.

### 3.8 `agent/orchestrator.py`
States and allowed moves:
```
NEW -> INTAKE
INTAKE -> PLAN_CHECK (plan photo uploaded) | RESEARCH
PLAN_CHECK -> RESEARCH
RESEARCH -> CALLING
CALLING -> HOLDING (best-fit clinic put on hold) | CALLING (next clinic) | QUOTES_IN
HOLDING -> CALLING (other clinics) -> NEGOTIATING (resume the held call)
QUOTES_IN -> NEGOTIATING | OFFERS_READY
NEGOTIATING -> OFFERS_READY
OFFERS_READY -> CALLBACK
CALLBACK -> BOOKED | SAVED
any -> ERROR
```
`advance()` raises on an illegal move. Every move emits an event to the browser.

### 3.9 Web app
- Rebuild the approved prototype's look and flow in React: dark background with a dot grid, lime `#D4FF4F` accent, lavender `#A99BFF` for the clinic side, Instrument Serif headlines, Geist text, the mic orb, the three-panel mission screen, the callback screen and the savings breakdown.
- `audio.ts` unlocks audio on the first tap (browsers block sound until a click).
- A small header shows the demo mode: Watch, Be the clinic, Real call (only visible if enabled).

### 3.10 CLI (for testing pieces before the UI exists)
```
agent intake-text "I want braces near 76010, under 5000, monthly or pay in full, I have an HSA"
agent simulate-call --clinic parkway --text-only
agent negotiate --fixture tests/fixtures/transcripts/three_quotes.json
agent run-demo --mode watch --text-only
```

---

## 4. Build order (Oct 26 to Nov 4)

Times are Central. Kickoff is Oct 26 at 11:00 AM Central (12:00 PM ET). Submissions are due Nov 4 at 8:00 PM Central (9:00 PM ET).

| Day | Goal | Done when |
|---|---|---|
| Sun Oct 26 | Kickoff, read the official rules, `git init`, keys working | `meta_llm.call_json` returns valid JSON from Muse Spark and Muse Wonder |
| Mon Oct 27 | Schemas, demo clinics, simulator | `agent simulate-call --text-only` runs a full text call with one demo clinic |
| Tue Oct 28 | Caller, extractor, ranker | Three text calls produce three Quotes with evidence |
| Wed Oct 29 | Negotiator, hold and resume, orchestrator (CodePath 7 to 9 PM) | `agent run-demo --text-only` shows the hold, the negotiation and the savings |
| Thu Oct 30 | Voice: STT, TTS, audio bus | A full call is audible end to end in the terminal or a test page |
| Fri Oct 31 | Web app: Intake and Mission screens over WebSocket | Watch mode works in the browser |
| Sat Nov 1 | Callback and Done screens, Be the clinic mode (keep evening free for a16z) | A judge can play a clinic with their mic |
| Sun Nov 2 | Deploy to Railway and Cloudflare Pages, fallbacks, stretch: real call | Live URL works on a fresh browser |
| Mon Nov 3 | Record the demo video, write `docs/submission.md` | Video exported |
| Tue Nov 4 | Buffer and submit before 8:00 PM Central | Submitted |

Cut list if behind (cut from the top): real phone call, plan photo reading, callback voice (keep the screen), Be the clinic mode (keep Watch).

---

## 5. Rules to follow

### Hackathon rules (as known on Oct 24; re-check the official rules on Oct 26)
- Startup track only. A project can be entered in one track.
- Submit through the Cerebral Valley portal by Nov 4, 9:00 PM ET, with a working demo and a demo video.
- Original work that you own. Open-source code is fine if you follow its license. AI coding tools are not banned in what has been published.
- Start the repo on or after Oct 26. Do not copy code from Kredisco or older projects. Reusing the prototype's design and script ideas is fine; rebuild the code.
- You keep ownership, but Meta and Cerebral Valley get a license to use the submission, including for marketing. Anything shared during the event is not confidential.
- If you use Muse Code, use Standard mode, not Contributor mode.
- Judging: Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%.

### Safety and legal rules for this build
- Real calls only to numbers on `ALLOWED_CALL_NUMBERS` that agreed to be called, and only with `ENABLE_REAL_CALLS=true`. AI-voice calls fall under the TCPA, so never call people or businesses that have not agreed.
- Every call opens with the AI disclosure and the recording notice.
- No real patient data in code, tests, fixtures, logs or commits.
- No medical judgment, ever.
- API keys live only in `.env`, never in code or commits.

---

## 6. Contents for `CLAUDE.md`

```markdown
# Patient Agent (Meta hackathon build)

Voice AI patient agent named Lana: intake by voice, calls clinics, gets all-in quotes, negotiates, books, calls the patient back.
Full spec: SETUP.md. Decisions: docs/decisions.md. Demo flow: docs/demo-script.md.

## Rules
- Fixed pipeline: orchestrator.py decides the next step. AI runs only inside agent/, clinics/ and voice/ steps that need language.
- Ranking, negotiation target, call order and hold/resume are plain Python. AI never picks the winner or changes the order.
- All AI calls go through integrations/meta_llm.py. Data-producing calls return Pydantic-validated JSON.
- Use Meta models only for AI (MODEL_FAST, MODEL_SMART, MODEL_STT from settings). Never hard-code model names or keys.
- Prompts live in app/prompts/*.md, never in Python strings.
- Every checklist answer stores speaker, exact words and timestamp. Unknown stays None and shows as "Not confirmed".
- Negotiation uses only real quotes actually received. Never invent prices, competitors or reasons. Never commit the patient beyond their preferences.
- No medical judgment. Every call opens with the AI disclosure and recording notice.
- The agent is Lana: female voice (VOICE_AGENT), introduces herself by name, labeled LANA in the UI. Her name comes from AGENT_NAME in settings, never hard-coded in prompts.
- The simulator's PriceSheet is hidden from the agent. Only clinics/simulator.py may read it.
- Real calls only when ENABLE_REAL_CALLS=true and the number is in ALLOWED_CALL_NUMBERS.
- No real patient data anywhere. Tests must not hit the network unless marked @pytest.mark.live.
- Money is whole dollars as int. Times are timezone-aware in TIMEZONE (America/Chicago).
- Every voice step needs a fallback (typed input, on-screen text, visible error). The demo must never freeze.
- Match the prototype design: dark dot-grid background, lime #D4FF4F, lavender #A99BFF for the clinic side, Instrument Serif + Geist + Geist Mono.
- No em dashes in UI copy.

## Commands
- Backend: cd backend && uvicorn app.main:app --reload
- Web: cd web && npm run dev
- Tests: cd backend && pytest
- Text-only demo: agent run-demo --mode watch --text-only

## Current day
Oct 26: keys and meta_llm.py. Update this line each day.
```

---

## 7. Setup commands for `README.md`

**Windows (PowerShell)**
```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy ..\.env.example ..\.env   # then add your keys
uvicorn app.main:app --reload

# new terminal
cd web
npm install
copy .env.example .env
npm run dev
```

**macOS**
```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env      # then add your keys
uvicorn app.main:app --reload

# new terminal
cd web
npm install
cp .env.example .env
npm run dev
```

---

## 8. After the hackathon

This build is for the demo. After Nov 9, the product is rebuilt from scratch by hand: web and mobile (Expo), real clinics and demo clinics behind a toggle, email outreach, after-hours scheduling, consent and redaction, and a price database from negotiated quotes. That gets its own setup file.
