# Decisions

Newest first.

## 2026-11-01: Be the clinic: a judge plays Parkway by voice

- **Entry.** "Let me be the clinic" during intake (the server sends `command: clinic`) or the quiet "or be the clinic" link creates a new case with `POST /cases {mode: "clinic"}`. It always has 2 clinics and the fixed clinic-mode patient in `demo_patients.json` (`mode: "clinic"`, never picked for Watch): braces, Arlington, under $5,000, evenings, pay in full OK. With those preferences Parkway (evening hours) is called first, then a simulated Sunrise.
- **Briefing.** The socket opens with `listen`. Lana says the briefing on Home, and the script card shows Parkway's prices. "Ready", "let's go" or a plain yes (or the Ready button) starts the calls; anything else gets "Just say ready when you want me to call."
- **JudgeLine** (`clinics/judge_line.py`) has the simulator's `next_line` interface. The orchestrator takes a receptionist factory: Parkway gets a JudgeLine, the others get ClinicSimulator. The judge's turns come from the same patient session (Meta endpointing, the grace merge, barge-in, STT seconds counted against the case). A turn starts once Lana's line has played in the browser: in clinic mode the line clock also times the voiced call lines. Turns heard early (a barge-in, or more words while Lana thinks) are kept and become the next reply. No floor check applies to the judge; the extractor and the negotiator still keep only their exact words.
- **The judge picks up first.** When Parkway's call connects, Lana waits for "Parkway Orthodontics, how can I help?" (up to `JUDGE_SILENCE_S`) before her disclosure. This uses the caller's optional `answer()` hook, so simulated clinics are unchanged.
- **Silence.** After `JUDGE_SILENCE_S` (12 s) with no turn and no speech, Lana says "Sorry, I didn't catch that." once. After a second silence she says "No worries, I'll call back another time. Thanks!" and the call ends (`CallEnded`). The quote stays partial, the run goes on, and that clinic is never called back.
- **Hold.** The judge's answer to the hold request is any words, or nothing after `JUDGE_HOLD_S` (8 s); a silent hold adds no clinic line. While on hold the server stops listening and the browser pauses the mic (`judge_hold` events), and the Sunrise call plays.
- **Events.** `judge_turn_start` carries a phase: greeting, price, fees, monthly, opening, hold or negotiation. A `wrap` phase follows the end of the call and is only a hint ("Thanks, bye!"); nobody waits for it. The judge's lines go out as `transcript_final` with `judge: true` and no audio, so they show at once and are never voiced.
- **Negotiation** is unchanged: a no ends that move and Lana tries the next; there is no repeat ask. A hang-up mid-negotiation keeps what was won and counts the unanswered move as neither won nor refused.
- **The end.** There is no callback in clinic mode. Done shows the ranked result and "You played Parkway": Parkway's final price and each concession with the judge's words and the second in the call.

## 2026-11-01: Lana understands people: no fillers, time of day, notes, honest booking

- **No spoken fillers.** "Got it." / "Mm-hm." played at the wrong moments, so they are gone from the server, the prompt file and the TTS prewarm. While Lana works on her reply the browser shows it instead: a slow primary-color ring pulses around the mic (fades in and out in 200 ms, a steady ring with reduced motion) and the caption row says "Lana is thinking" in muted text.
- **Intake model, chosen by testing.** New settings `INTAKE_MODEL` (blank means `MODEL_FAST`) and `INTAKE_REASONING` (default `low`). A scratchpad eval ran 12 messy answers through the real intake turn (each after the question it answers, e.g. "no, I just want mornings" right after the must-haves question). Every setting understood all 12 (fields, notes, answered topics). Latency per turn, including any retry:

  | Setting | Correct | Median | p90 | Retries | Notes on the replies |
  |---|---|---|---|---|---|
  | muse-spark-1.3 @ minimal (run 1 / run 2) | 12/12, 12/12 | 3.0 s, 3.1 s | 4.1 s, 3.8 s | 1, 1 | Natural, a little terse ("Not driving, got it."); a retry costs 4 to 8 s on that turn |
  | **muse-spark-1.3 @ low** (run 1 / run 2) | 12/12, 12/12 | 4.0 s, 3.9 s | 4.8 s, 4.9 s | 0, 0 | Warm and natural ("That's understandable, a lot of people feel that way.") |
  | muse-spark-1.3 @ medium | 12/12 | 6.3 s | 9.1 s | 0 | Natural; up to 13 s on "whatever's cheapest" |
  | muse-wonder-1.0 (its own level) | 12/12 | 7.9 s | 13.5 s | 1 | Most concise, best notes ("leaning toward clear aligners"); up to 21 s |

  Decision: **muse-spark-1.3 at low** stays the default. It was right every time with no retries, and is under a second slower than minimal at the median. Medium and Wonder are too slow for a voice turn.
- **Free-form understanding.** `Preferences` has `time_pref` ("morning", "afternoon", "evening", "weekend", "any"; default "any") and `notes` (short plain phrases such as "nervous about pain", "for her 15-year-old son"). The model returns updates, notes, answered topics, the reply and done; a time of day answers the must-haves topic. Python only guards: budget parsing, a monthly cap ("two fifty a month" means monthly payments plus the note "up to $250 a month", never a total), pay in full only when clearly said, city matching, no medical advice, no em dashes, no praise openers, at most 2 sentences. A "morning hours" must-have also sets `time_pref` (the bug that booked an evening slot). Notes are kept across turns, deduped, at most 5 of at most 10 words.
- **One retry before the fixed question.** If Lana's line fails a check (including asking an answered topic) the model is asked once more with the reason; only if that fails too is the fixed question said. A medical question still goes straight to the fixed no-advice line.
- **Confirmation** is built from the real preferences: "Got it. Braces near Plano, under $5,000, mornings. I noted: wants Spanish-speaking staff. I'm calling clinics now...". At most one note, one a clinic can answer if there is one.
- **Demo clinics have real time differences.** Parkway: weekday evenings plus a Saturday morning; Sunrise: weekday mornings; Lakeside: Saturday mornings and weekday afternoons. Slots use the weekday form so a weekday never lands on a weekend. Each price sheet has a few `facts` (languages, aligners, comfort options, paying for a minor); the receptionist answers only from them and otherwise says she'd need to check.
- **Researcher** reads each clinic's public hours and booking-page openings (`loader.public_openings`, no prices). Fit is the average of the must-haves and, when the patient has a time, the share of openings in that time (hours decide when none are listed). The log says why: "Calling Sunrise first, they have morning openings and 4.6 stars".
- **Calls.** With a time preference Python asks "Do you have any morning openings next week?" instead of a generic slot question. At most one note question per call, after the checklist, for the most important note a clinic can answer (language, then a minor's payment, then pain or sedation, then aligners; a must-have like "spanish speaking" counts). The extractor stores the answer as a sourced `note_answer` with the clinic's exact words; if the clinic didn't say it, it stays not confirmed.
- **Booking is honest.** `book()` takes the earliest slot in the patient's time. If none is, it books nothing and returns `NoMatchingSlot`; on the callback Lana says "They don't have mornings. The earliest is Tuesday, November 3 at 5:30 PM. Want that, or should I hold off?" and only a yes books it. The summary names the opening in the preferred time and at most one note answer, only as the clinic gave it.

## 2026-11-01: Turn taking, barge-in, honest over-budget results, any city

- **Grace period.** After Meta ends a turn (speechComplete), the server waits `END_GRACE_MS` (default 700) before acting. Another final in that window joins the same turn. A partial means the patient is talking again, so the commit waits for that utterance's final (or 3 s more if none comes). The live caption keeps the text from before the pause.
- **Fillers** play only if the reply isn't ready 1.5 s after the turn is committed: once per turn, never the same twice in a row, never on the first turn. No praise words among them.
- **Barge-in.** While Lana talks to the patient (not during clinic calls) the mic stays on, with echo cancellation, but sends nothing. If a frame's RMS stays at or above `BARGE_IN_RMS` (0.06) for 3 frames (300 ms), the browser stops her audio, sends `barge_in`, then sends the held 300 ms and goes live. The server cuts the line in progress (no more chunks; its last frame says `cut: true`), skips the rest of that turn's lines, and treats what comes next as a new turn. The threshold sits above echo-cancelled speaker bleed from a laptop; "About this demo" recommends headphones.
- **No praise openers.** The intake prompt says acknowledge only to add information or after something personal. Python strips a leading "Perfect", "Great", "Awesome", "Wonderful", "Excellent", "That's good" and the like.
- **Over budget is shown, not hidden.** The ranker no longer drops offers over `budget_max`. They carry `over_budget_by` and rank after every offer within budget. If even the best is over, the callback says so first ("Nobody would go under $3,000. The best I got was $3,990 at Parkway, $990 over your budget.") and asks "Want me to book it anyway, or hold off?". The budget and the overage are allowed amounts in the summary's number check. Done shows "Over your budget by $X". "hold off", "don't book it" and "book it anyway" are keywords, so no classifier call is needed.
- **Any city.** A place that isn't one of the 24 DFW cities is kept as `Preferences.place`. Lana says once: "This is a demo, so I'll use demo clinics placed near {place}. Real clinics come next." The same 3 demo clinics, distances and prices are used; the log says "Found 3 demo clinics near {place}", and the summary facts say they are demo clinics, not real ones there. Fuzzy city matching now needs a similar length for a spelling match, so "San Francisco" no longer becomes Frisco.
- **Step away.** If the patient stepped away during the calls, the callback summary starts with "I made {n} calls while you were away." n counts dialed calls; resuming a held call isn't a new one.

## 2026-11-01: Conversational intake, no dead air, a voiced Watch intro, whole-line audio

- **Intake is a conversation, Python still owns the rules.** Each turn MODEL_FAST (`call_json`, `reasoning_effort=low`) gets the whole chat, the draft, the answered and missing topics and Lana's previous line, and returns `updates`, `answered`, `asking`, `reply` and `done`. Python merges the values exactly as before (budget parsing, pay in full only when clearly said, `match_city`, defaults). Her line is said only if it passes the checks: no medical opinion, no em dash, at most 2 sentences and one question, a missing topic (an important one while treatment, location or budget is missing), and not her previous line. Otherwise the fixed question for the first missing topic is said, or its `again_*` rephrase if that was her previous line.
- **What counts as an answer:** a value, the model's `answered` list, "skip"/"doesn't matter" for the asked topic, "near me"/"close by"/"nearby" (8 miles, "close by" added to must-haves, must-haves answered), and "no"/"nothing"/"that's it" after the must-haves question (must-haves empty). Lana may wrap up once treatment, location and budget are in and the patient sounds done; the rest takes the conservative defaults. At most 6 questions, then the confirmation (same style as before). Two unusable answers still set a topic's default.
- **No dead air.** If Lana's reply isn't ready 400 ms after the patient's turn ends, she says a cached filler first ("Got it.", "Mm-hm.", "Okay.", "Sure.", never the same twice in a row). Intake phase only.
- **Watch mode opens with a demo patient** (`data/demo_patients.json`, 4 scenarios, one picked at random). Their request is voiced (`VOICE_PATIENT`, blank falls back to `VOICE_CLINIC_1`) and captioned on Home, Lana answers with her real intake (one turn, or two if she needs one more thing; the second confirms with defaults), and the calls start with what she heard. If intake fails, the scenario's own preferences are used. The patient line is a `user_said` with `voiced: true`, so `agent ws-watch` still validates every event; no `intake` payload is sent in Watch mode for the same reason.
- **Log lines are built from real data**, e.g. "Parkway is $5,050 all in. That's the best fit so far, so I'm asking them to hold while I check Sunrise", "Sunrise is $4,200 all in, $850 less than Parkway", "Going back to Parkway to ask them to match Sunrise's $4,200". They keep the "Calling X first" and "Then X" openings the browser reads the call order from.
- **Call openings rotate.** The first price question is one of 4 wordings in Python (never the last call's), so calls sound different, start without a model call, and the disclosure plus opening is cached by the TTS prewarm. The disclosure is unchanged.
- **Cut-off lines.** Cause: the player stopped any line still playing 15 s after it started, which cut long lines such as the callback summary. The cap is now the decoded length plus 3 s. Also, a cache miss is now received whole before it is sent: a line that broke off mid-stream used to be sent (and played) cut short; now it is requested once more, and if that fails it is shown as text. The browser already decoded a line only after its last frame, so this adds no delay. Words reveal by character weight, and a dev-only console line per line shows chars, bytes, decoded and played seconds.

## 2026-10-31: Patient mode end to end, STT per turn, fuzzy cities, callback

- **STT only while the patient may talk.** The Meta realtime reference (https://dev.meta.ai/docs/api-reference/voice/realtime, checked today) has no pause: the only client control message is `endStream`, and ingress slower than real time is closed with 1008. So `PatientSession` opens a Meta session on the first mic frame after Lana's line, closes it (`endStream`, flush) as soon as a turn is heard, Lana talks or thinks, or no mic frame comes for 3 s, and reopens on the next frame. Frames that arrive while Lana has the turn are dropped, never sent. Silence that fills a late frame inside an open session keeps the stream real time but is not charged.
- **`MAX_STT_SECONDS_PER_CASE` counts real mic audio only**, default raised from 180 to 300 s.
- **No vocabulary hints.** The realtime handshake documents only `authorization`, `audioEncoding`, `model`, `mode` (plus our `partialMode` and `emitAudioProgress`); there is no vocabulary, keyword, phrase-boost or prompt field. City names are therefore matched after transcription.
- **Fuzzy DFW cities** (`intake.match_city`): 24 cities with a ZIP each. An exact name wins (any case, spaces optional). Otherwise difflib spelling ratio ≥ 0.8, or a sound-key ratio ≥ 0.85 (similar consonants merged, vowels dropped) with keys of 4+ letters and spelling ratio ≥ 0.4. "LinkedIn" → Arlington; "color", "garden", "near me" match nothing. The raw reply is scanned only when Lana asked for the location and the model found no place. A fuzzy match is confirmed in her next line ("Arlington, got it.").
- **Never a third ask.** Two unusable replies to the same question set its default (Arlington, no budget cap, not sure, no x-rays, no must-haves) with a one-line notice.
- **Patient mode:** intake done → Lana's line, then the calls start at once with the patient's Preferences and the Home clinic toggle. The mic stays open during the calls, but only step-away phrases ("I'll wait", "call me back") do anything; clinic speech is ignored without an LLM call. Stepping away (voice or tap) turns call audio off server side (`record.voice`), so no TTS is spent and the calls finish sooner; the browser mutes and fast-forwards.
- **Callback:** after OFFERS_READY the orchestrator moves to CALLBACK, writes Lana's summary with MODEL_SMART (`prompts/summary.md`) and emits `callback_started`. Python checks every dollar amount against the quotes and the negotiation; anything else means the template summary. The browser rings, answers after 3 s and sends `answer`; Lana says the summary plus "Want me to book it?". Yes → BOOKED at the winner's earliest confirmed slot ("This is a demo clinic, so no real appointment was made."); not yet → SAVED. Two unclear answers save the quote: Lana never books without a clear yes. No confirmed slot → saved, not booked. Watch mode uses the same callback; its mic is used only if the site already has mic permission.
- **Live price drops:** `negotiate(on_concession=...)` fires the moment the clinic agrees, so `concession_won` reaches the browser during the call.
- **`DEMO_CHECKLIST`** (default `total_price,records_fee,monthly_payment,next_slots`) limits what Lana asks on a call; the extractor still records any other field the clinic mentions.
- **BEST ties** are decided in the browser before offers arrive: the clinic Lana negotiated with wins a price tie (Python picks it as the best on everything but price, so it matches the ranker), then the higher rating. After offers_ready the rank 1 offer has the badge. A new event type was avoided because `agent ws-watch` validates every event against the schema.

## 2026-10-31: Cases in memory only, and demo usage limits

- Hackathon build keeps cases in memory only, no database; nothing is stored after the session. Cases expire 30 minutes after their last use (`db/store.py`). No audio or transcripts are written to disk. `DATABASE_URL` is unused for now.
- Usage limits live in `guard.py`, set by `MAX_RUNS_PER_CASE` (2), `MAX_CASES_PER_IP_HOUR` (5), `MAX_LLM_CALLS_PER_CASE` (60), `MAX_TTS_CHARS_PER_CASE` (4,000) and `MAX_TTS_CHARS_PER_DAY` (30,000). TTS characters count only cache misses, since cached lines cost no credits. Counters are in memory and reset when the server restarts.

## 2026-10-31: Muse Voice Transcribe, streaming STT in Endpointing mode (voice/stt.py)

Sources: Meta Model API docs: Speech to text guide, https://dev.meta.ai/docs/speech-to-text; Realtime reference, https://dev.meta.ai/docs/api-reference/voice/realtime; File transcription, https://dev.meta.ai/docs/api-reference/voice/transcribe; Schemas, https://dev.meta.ai/docs/api-reference/voice/schemas.

- **Auth on the WebSocket:** the HTTP `Authorization` header is ignored. The key goes in the handshake, which must be the first JSON text frame and must arrive within 10 s of connecting: `{"authorization": {"accessToken": "Bearer <key>"}, "audioEncoding": "PCM_24KHZ", "model": "<MODEL_STT>", "mode": "ENDPOINTING", "partialMode": "CUMULATIVE", "emitAudioProgress": true}`. The ack is the only server frame with no `type`, and it carries `sessionId`. An optional `?sessionId=` query parameter sets the ID used in Meta's logs; we send `stream-<uuid>`.
- **Audio format:** raw signed 16-bit little-endian mono PCM in binary frames (no base64). The encoding is `PCM_24KHZ` (native, preferred, 48,000 bytes/s) or `PCM_16KHZ` (resampled on Meta's side). There is no required chunk size; Meta's example sends 80 ms frames at real time. We use 24 kHz in 100 ms chunks (4,800 bytes): `stt.AUDIO_FORMAT` and `stt.CHUNK_MS`, which the browser and the tests use too.
- **Modes**, set by `mode` in the handshake. A session's mode can't change once it starts.
  - `PUSH_TO_TALK` (default): one turn, and the client marks the end, for example with a mic button. Completion is `transcript` with `final: true`.
  - `ENDPOINTING`: Meta detects turn boundaries, one turn per speech segment. Completion is `speechComplete` per `turnId`. **We use this one.** The docs give no silence threshold or sensitivity setting.
  - `DIARIZATION`: endpointing plus `speaker` events with session-scoped labels (`A`, `B`). The docs say it is "not tuned for low-latency voice commands". It could suit phone audio later, but not live turns.
- **Server events** (dispatch on `type` and ignore unknown types):
  - `speechStart {turnId}`.
  - `transcript {transcript, final}`: partials have `final: false` and no `turnId`, and belong to the latest `speechStart`. In CUMULATIVE mode each partial is the full hypothesis so far.
  - `speechEnd {turnId}`: a boundary only, no text.
  - `speechComplete {turnId, transcript}`: the post-processed text of the turn. It can differ from the last partial; in our test the partial "Under 5,000 and I need evenings" came back with a period added.
  - `audioProgress {audioProcessedMs}`, `error {message, sessionId}` (sent before a fatal close), and `speaker` (Diarization only).
- **Our mapping:** partial `transcript` → `Partial`; `speechComplete` → `Final` then `EndOfTurn`. `EndOfTurn` comes after `Final`, so the consumer has the text when it acts. Meta sends `speechEnd` and `speechComplete` together (same millisecond in our runs).
- **Ending a session:** send `{"type": "endStream"}`, then keep reading until Meta closes with 1000. Closing early can drop pending events. After endStream, Meta also sends `transcript` with `final: true` and empty text; we ignore it.
- **Close codes:** 1000 normal; 1008 invalid request or pacing problem (more than 5 s backlog, or audio arriving slower than real time); 1011 backend failure, including "Max session duration reached"; 1013 rate limited.
- **Limits:** 60-minute sessions with no resume token; handshake within 10 s; no more than 5 s of audio queued ahead of processing; idle input without endStream gets closed; 256 concurrent streams and 16,000 per hour, shared with the file endpoint.
- **File endpoint:** `POST {META_BASE_URL}/asr/transcribe` with `Authorization: Bearer`. The body is multipart with a `request` JSON part (`{"model", "audioEncoding": "WAV", "mode"}`) and an `audio` part holding a mono 16-bit PCM WAV at 16 or 24 kHz. The response is `{sessionId, transcript, audioDurationMs, turns}`. Limits are 32 MB (413) and 10 minutes (400); 429 means rate limited. `transcribe_file` uses `PUSH_TO_TALK`, so the whole clip is one turn.
- **End of turn comes from Meta.** In `hello.wav` the speech ends at about 4.5 s. Meta put `speechEnd` at about 4,540 to 4,620 ms and usually sent `speechEnd` and the final text once it had processed about 5,040 to 5,120 ms, which is 540 to 620 ms after speech stopped. On the wall clock that was about 0.72 to 0.79 s after our last voiced chunk went out. In some runs it took longer than 700 ms, and Meta's partials can trail its `audioProcessedMs`. Connect (handshake ack) took 0.8 to 1.2 s; the first partial came 0.7 to 0.8 s after the first audio.
- **Silence fallback, never racing Meta** (chosen after a strict 700 ms rule cut "...and I need" before "evenings" in one run):
  - **700 ms** only for a turn Meta never opened with `speechStart`, meaning its endpointing isn't working.
  - **2 s safety net** for a turn Meta opened but never ended with `speechEnd`.
  - Silence is measured against Meta's `audioProcessedMs`, from the end of our last voiced chunk (RMS of at least 500 out of 32,767), and only while there is partial text. After `speechEnd` we wait for `speechComplete`.
  - After a fallback, later partials and `speechComplete` for that turn are ignored, so there is never a second `Final`.
  - A turn still open when the session ends (after endStream) is also sent as `Final`, marked as a fallback.
- **Reconnect:** one reconnect per session, for a failed first connect or any drop (any close other than a 1000 after endStream). The new session starts fresh, and a partial from the old session is logged and dropped. A second failure raises `STTUnavailable`, and the UI should switch to tap buttons. We also retry once on 1008, though Meta says that one fails again.

## 2026-10-31: TTS model per speaker, and a reworded disclosure

- Lana uses `eleven_v4_turbo` (`ELEVENLABS_MODEL_AGENT`) with a `[warmly]` audio tag (`ELEVENLABS_TAG_AGENT`) in front of each line for a more natural, expressive voice. The tag is sent only when her model supports audio tags and is never shown on screen.
- Clinics use `eleven_flash_v2_5` (`ELEVENLABS_MODEL_CLINIC`) because it is cheaper and faster, and it sounded better for the clinic voices in a side-by-side test against `eleven_v4_turbo`.
- `ELEVENLABS_MODEL` still works as a fallback for either speaker when set. The model and the tag are part of the TTS cache key.
- The disclosure was reworded to sound natural: "Hi there! This is Lana, I'm an AI assistant calling for a patient. Just so you know, this call's recorded." It still states that Lana is an AI and that the call is recorded. The name comes from `AGENT_NAME`.

## 2026-10-31: eleven_v4_turbo, voice settings and audio tags

Sources: ElevenLabs Models, https://elevenlabs.io/docs/models; Stream speech API reference, https://elevenlabs.io/docs/api-reference/text-to-speech/stream; prompting guide (audio tags, covers Eleven v4), https://elevenlabs.io/docs/best-practices/prompting/eleven-v3; Text to Dialogue stream, https://elevenlabs.io/docs/api-reference/text-to-dialogue/stream; plus a live `GET /v1/models` with our key.

- Model ID is `eleven_v4_turbo` (released with `eleven_v4`). The Models page calls it the real-time model, with a median latency of about 100 ms not counting the network, and mentions only "the Text to Dialogue websocket" as where it's available. `eleven_turbo_v2_5` and `eleven_turbo_v2` are now deprecated.
- Streaming through the Python SDK: `GET /v1/models` reports `can_do_text_to_speech: true` for `eleven_v4_turbo` (10,000 characters per request). The same `client.text_to_speech.stream(voice_id, text=..., model_id="eleven_v4_turbo", voice_settings=..., output_format=...)` call we already use works: tested live, HTTP 200, mp3 streamed. No code path change was needed.
- Audio tags: v4 Turbo lists "Audio tags for fine-grained control". The syntax is inline square brackets, for example `[laughs]`, `[whispers]`, `[sighs]`, `[curious]`, `[excited]`. SSML `<break>` is not supported on v3 or v4 (third-party source, not confirmed in the official docs).
  - `tts.AUDIO_TAG_MODELS` = `eleven_v3`, `eleven_v3_conversational`, `eleven_v4`, `eleven_v4_turbo`. For any other model (for example flash), tags are stripped before sending so they are never read aloud.
  - On screen, always show `tts.strip_audio_tags(text)`.
- Voice settings (TTS API reference): `stability` (default 0.5), `similarity_boost` (0.75), `style` (0), `use_speaker_boost` (true), `speed` (1.0; the prompting guide gives 0.7 to 1.2). New settings `ELEVENLABS_STABILITY` 0.4, `ELEVENLABS_SIMILARITY` 0.8, `ELEVENLABS_STYLE` 0.3, `ELEVENLABS_SPEAKER_BOOST` true are sent with every line, and they are part of the cache key, so changing them makes new audio. Old cache files (keyed without settings) are simply never hit again.
- Caveat: `GET /v1/models` reports `can_use_style: false` and `can_use_speaker_boost: false` for `eleven_v4_turbo`, `eleven_v4`, `eleven_v3` and `eleven_flash_v2_5`, so `style` and speaker boost are probably ignored by these models. The API accepted them without an error. Stability 0.3 and 0.4 were accepted on v4 Turbo (no fixed 0 / 0.5 / 1 steps).
- `agent say` takes `--model` and `--stability` to compare without editing `.env`.
- First-chunk latency measured today (Lana disclosure, 83 characters, new connection each run): flash 0.58 s, v4 Turbo 0.69 s, v4 Turbo with stability 0.3 1.44 s. Parkway price line (45 characters): flash 0.81 s, v4 Turbo 0.82 s. One run each, so treat these as rough. Not decided yet which model to use; the default stays `eleven_flash_v2_5` until we listen.

## 2026-10-31: ElevenLabs streaming text to speech (voice/tts.py)

Sources: ElevenLabs API reference, Stream speech, https://elevenlabs.io/docs/api-reference/text-to-speech/stream; Get user subscription, https://elevenlabs.io/docs/api-reference/user/subscription/get; SDK README, https://github.com/elevenlabs/elevenlabs-python. The API reference shows only raw HTTP examples, so the SDK call below was confirmed in the installed SDK (`elevenlabs` 2.74.1, `text_to_speech/client.py`).

- Endpoint: `POST /v1/text-to-speech/{voice_id}/stream?output_format=...`, auth header `xi-api-key`.
- SDK call we use:
  ```python
  client = AsyncElevenLabs(api_key=..., timeout=8)
  async for chunk in client.text_to_speech.stream(
      voice_id, text=text, model_id="eleven_flash_v2_5", output_format="mp3_44100_128",
      request_options={"timeout_in_seconds": 8, "max_retries": 0},
  ):
      ...  # bytes
  ```
  On the async client, `stream` is an async generator: iterate it, don't `await` it.
- Model comes from the new setting `ELEVENLABS_MODEL` (default `eleven_flash_v2_5`). Output is `mp3_44100_128` (the API default; 192 kbps needs Creator tier).
- Timeout: 8 s for the request and 8 s for each wait on the next chunk. SDK retries are off. On any failure `stream_speech` logs and stops, so the caller shows the text only. Audio already yielded before a mid-line failure can't be taken back, and a partly received line is never cached.
- Cache: `backend/.cache/tts/<sha256(voice_id, model, text)>.mp3`, joined with newlines so the parts can't run together. Only fully received lines are written, through a temp file. `agent tts-prewarm` caches Lana's disclosure, greeting, the 6 intake questions and the hold request.
- Usage: `client.user.subscription.get()` returns `character_count` and `character_limit` (needs a key with User read access). Our key is Starter tier, 38,417 characters a month.
- Voice IDs: our voices come from the Voice Library. `voices.get(id)` returns 400 `voice_not_found` for a library voice until it has been used once, but text to speech still works with the ID. `agent tts-check` falls back to `voices.get_shared(search=id)` to confirm those.
- Measured latency (first chunk): about 5 to 6 s the first time a library voice is ever used, 0.75 s on a new connection afterwards, 0.13 s on a warm connection. So: one SDK client per event loop (connection reuse), and the server should prewarm or make one request per voice at startup.

## 2026-10-29: reasoning_effort="minimal" for live call turns (CALL_REASONING)

Source: Meta Model API docs, Reasoning guide, https://dev.meta.ai/docs/reasoning (re-checked today; matches the 2026-10-28 entry below).

- Chat Completions takes a top-level `reasoning_effort`. Reasoning cannot be turned off: `none` returns HTTP 400 on Muse Spark and Muse Wonder. The lowest level is `minimal`, "Shortest reasoning pass."
- `meta_llm.stream_chat` and `call_json` take an optional `reasoning` argument. When it is `None`, the parameter is left out and the model picks its own level, which is how every call worked before.
- New setting `CALL_REASONING` (default `minimal`) sets the level for live call turns. Demo receptionist turns (`clinics/simulator.py`) use it now.
- `meta_llm` now logs `usage.completion_tokens_details.reasoning_tokens`.
- Measured on `muse-spark-1.3` receptionist turns at `minimal`: 31 to 337 reasoning tokens per turn (most under 65), 0.9 to 3.1 s. A five-word ping without the setting used about 830 completion tokens. The price-match turn reasoned longest.

## 2026-10-28: Hidden reasoning tokens and the reasoning_effort setting

Observed: a five-word `agent ping` reply used about 850 completion tokens on `muse-spark-1.3` and 280 on `muse-wonder-1.0` (prompt was 14 tokens). Latency was 4.2 s and 3.4 s. This is likely hidden reasoning, which costs money and adds latency on every live call turn.

Source: Meta Model API docs, Reasoning guide, https://dev.meta.ai/docs/reasoning (also https://dev.meta.ai/docs/models).

- Chat Completions takes a top-level `reasoning_effort`. The Responses API nests it as `reasoning.effort`. The guide does not document one for the Messages API.
- Values: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.
- If the setting is left out, the model still reasons at a level it picks itself. That is what our calls do today.
- Reasoning cannot be turned off for our models: `none` is "Not supported by Muse Wonder or Muse Spark" and returns HTTP 400. The lowest level is `minimal`.
- `muse-spark-1.3` and `muse-wonder-1.0` both support `max`. `muse-spark-1.3-contributor` does not, which doesn't matter because we never use contributor IDs.
- Reasoning tokens show in `usage.completion_tokens_details.reasoning_tokens` (Chat Completions) and are billed at the output rate. They count toward `max_tokens`, so a low `max_tokens` can cut the visible reply short.
- Higher effort means more latency and cost. When streaming, the first visible chunk comes only after reasoning finishes. Raw reasoning is never streamed.
- Not decided yet; code unchanged. A likely next step: send `reasoning_effort="minimal"` (or `low`) for `MODEL_FAST` live call turns, leave it at the default or higher for `MODEL_SMART` work, and log `reasoning_tokens`.

## 2026-10-28: Meta Model API endpoint and model IDs confirmed

Source: Meta Model API docs, https://dev.meta.ai/docs/ (ai.developer.meta.com/docs redirects there).
Pages checked: https://dev.meta.ai/docs/models, https://dev.meta.ai/docs/pricing-rate-limits,
https://dev.meta.ai/docs/api-reference/chat-completions/create-chat-completion,
https://dev.meta.ai/docs/image-understanding.

- Base URL: `https://api.meta.ai/v1`. Auth is a Bearer key in `Authorization`.
- Chat Completions (`POST /chat/completions`) is the OpenAI-compatible endpoint, so we use the `openai` Python SDK with a custom `base_url`.
- Model IDs, matching `SETUP.md` section 2:
  - `MODEL_FAST` = `muse-spark-1.3` (Standard mode; `1.2` and `1.1` also exist)
  - `MODEL_SMART` = `muse-wonder-1.0` (preview, 1,048,576-token context)
  - `MODEL_STT` = `muse-voice-transcribe-1.0`, realtime at `wss://api.meta.ai/v1/asr/realtime` (file transcription at `POST /v1/asr/transcribe`)
- Standard mode: "your content is not used to train Meta models." Contributor mode is chosen only by the `-contributor` model ID suffix (for example `muse-spark-1.3-contributor`). There is no header or parameter for it. **Never put a `-contributor` ID in `.env`.** Wonder and Voice Transcribe have no Contributor mode.
- Muse Spark and Muse Wonder accept the same requests on the same endpoints, including image input.
- Images in Chat Completions go in a `user` message as `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}`. Allowed types: jpeg, png, gif, webp, x-icon. Up to 50 MB per inline image and 50 images per request.
- Not confirmed in the docs: whether `response_format` / JSON mode works on Chat Completions. So `meta_llm.py` puts the JSON schema in the prompt and validates with Pydantic.
- Token usage is returned when streaming (seen in a live `agent ping` without `stream_options`), and `meta_llm.py` logs it.
- Timeout is 30 s per request, and the SDK's own automatic retries are off (`max_retries=0`) so one call never takes longer than 30 s and the demo never stalls. The only retry is `call_json`'s single retry for invalid JSON.
