"""FastAPI app: REST routes and the /ws/case/{id} WebSocket that streams a case to the browser.

On the same socket the browser can send mic audio as binary frames (AUDIO_FORMAT, 100 ms
each). A PatientSession turns them into transcripts, then into commands, intake answers, a
step away during the calls, or the yes / not yet of Lana's callback, and Lana speaks the
reply. Half duplex: the browser mutes the mic while Lana talks, and the server sends Meta
nothing during her lines (its session closes and reopens on the patient's next frame).

Watch mode opens the same way, with a demo patient (data/demo_patients.json) in place of the
mic: their request is voiced, Lana answers it with her real intake, and the calls start with
the preferences she heard.

Be the clinic opens with listen too: Lana briefs the judge, and on "ready" the calls start for
the clinic-mode demo patient. The judge's mic is Parkway's front desk (clinics/judge_line.py);
the other clinic is simulated. Their own words are shown, never voiced, and there is no callback.
"""

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app import guard
from app.agent.commands import Command as SpokenCommand
from app.agent.commands import match_command, parse_command
from app.agent.caller import opening_lines
from app.agent.intake import TOPIC_FIELDS, Intake
from app.agent.orchestrator import Orchestrator, Say, pick_slot, simulated, spoken_slot
from app.cli import DEMO_PREFERENCES
from app.clinics.judge_line import CALL_BACK_LATER, DIDNT_CATCH, JUDGE_CLINIC_ID, JudgeLine
from app.config import get_settings
from app.data.loader import get_public_clinics, load_demo_clinics
from app.db.store import CaseRecord, store
from app.integrations import meta_llm
from app.schemas.case import Case
from app.schemas.clinic import Clinic, PriceSheet
from app.schemas.enums import CallStatus, CaseStatus, DemoMode, Speaker
from app.schemas.events import (
    AgentSaid,
    CallStatusEvent,
    JudgeHold,
    JudgePhase,
    JudgeTurnStart,
    TranscriptFinal,
    UserSaid,
)
from app.schemas.preferences import TIME_PLURAL, Preferences
from app.voice import tts
from app.voice.audio_bus import AudioBus
from app.voice.stt import AUDIO_FORMAT, CHUNK_MS, Final, Partial, StreamingSTT, STTUnavailable
from app.voice.tts import VoiceConfigError, check_voices, voice_for, warm_up

log = logging.getLogger(__name__)
# uvicorn configures only its own loggers; this shows the app's INFO lines (warm-up, runs, limits).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

LIMIT_ERROR = {"type": "error", "code": "limit", "message": guard.LIMIT_MESSAGE}
# What the browser gets for anything unexpected; the details go to the server log only.
SERVER_ERROR_MESSAGE = "Something went wrong. Please try again."
SERVER_ERROR = {"type": "error", "code": "server_error", "message": SERVER_ERROR_MESSAGE}

# Lana's fixed lines to the patient. The full disclaimer stays in "About this demo".
GREETING = (
    "Hey, I'm {agent_name}, your personal health companion! Quick heads-up, this is a live demo, "
    "so I'll call demo clinics, not real offices. What do you need help with?"
)
SORRY = "Sorry, I missed that. Could you say it again?"
# The callback, after Lana's summary.
BOOK_QUESTION = "Want me to book it?"
BOOK_ANYWAY = "Want me to book it anyway, or hold off?"  # when even the best offer is over budget
BOOK_AGAIN = "Should I book it? You can say yes or not yet."
BOOKED_LINE = "You're booked at {clinic} for {slot}. This is a demo clinic, so no real appointment was made."
SAVED_LINE = "Saved, not booked. I'll hold this quote for you."
NO_SLOT_LINE = "They didn't give me an opening time yet, so I saved the quote instead. I'll hold it for you."
# When the winner has no opening in the patient's preferred time: said instead of BOOK_QUESTION.
NO_MATCH_QUESTION = "They don't have {times}. The earliest is {slot}. Want that, or should I hold off?"
# Watch mode: the demo patient's reply when Lana asks about something their scenario doesn't
# cover, and her line if intake fails (the scenario's own preferences are used then).
DEMO_DONE_LINE = "That's it, go ahead."
DEMO_FALLBACK_CONFIRM = "Got it. I'm calling clinics now. You can watch, or step away and I'll call you back."
# Be the clinic: Lana's briefing on Home, and her nudge when the judge says something other than ready.
BRIEFING = (
    "Great. You're the front desk at Parkway Orthodontics. I'm calling for a patient who wants braces "
    "near Arlington with evening hours. Answer however you like, the card shows your prices. Ready?"
)
READY_AGAIN = "Just say ready when you want me to call."
FIXED_LINES = [
    SORRY, BOOK_AGAIN, SAVED_LINE, NO_SLOT_LINE, DEMO_FALLBACK_CONFIRM,
    BRIEFING, READY_AGAIN, DIDNT_CATCH, CALL_BACK_LATER,
]
# A partial during the grace period means the patient is talking again; if Meta never sends
# that utterance's final, the turn is committed this long after the partial anyway.
GRACE_PARTIAL_EXTRA_S = 3.0

DEMO_PATIENTS_PATH = Path(__file__).resolve().parent / "data" / "demo_patients.json"


class DemoPatient(BaseModel):
    """One demo scenario: what the patient asks, what they answer if Lana asks more."""

    id: str
    # watch: a Watch mode patient. clinic: the patient Lana calls for in Be the clinic.
    mode: Literal["watch", "clinic"] = "watch"
    request: str
    answers: dict[str, str] = Field(default_factory=dict, description="Keyed by intake topic")
    preferences: Preferences = Field(description="Used if intake fails; Be the clinic always uses them")


def load_demo_patients(mode: Literal["watch", "clinic"] = "watch") -> list[DemoPatient]:
    patients = TypeAdapter(list[DemoPatient]).validate_json(DEMO_PATIENTS_PATH.read_bytes())
    return [p for p in patients if p.mode == mode]


def clinic_patient() -> DemoPatient:
    """Be the clinic's fixed patient: their preferences make Parkway the first call."""
    return load_demo_patients("clinic")[0]

# What the patient says to step away while the calls run (after _normalize).
STEP_AWAY_PHRASES = {
    "i'll wait", "i will wait", "i'll just wait", "i can wait", "step away", "i'll step away",
    "call me back", "just call me back", "call me when it's done", "call me when you're done",
}
# What the judge says to start the call in Be the clinic (after _normalize); a plain yes works too.
READY_PHRASES = {
    "ready", "i'm ready", "im ready", "i am ready", "ready let's go", "let's go", "lets go", "go",
    "ok", "okay", "let's do it", "call", "call them", "call me", "start", "start the call",
}


def _greeting() -> str:
    return GREETING.format(agent_name=get_settings().agent_name)


def _open_clients() -> None:
    """Create the Meta and ElevenLabs clients once, on the server's event loop."""
    for name, opener in (("Meta", meta_llm.get_client), ("ElevenLabs", tts._client)):
        try:
            opener()
        except Exception as err:
            log.warning("%s client not opened: %s", name, err)


async def _warm_up_voices() -> None:
    try:
        voices = check_voices(get_public_clinics())
    except VoiceConfigError as err:
        log.warning("TTS warm-up skipped: %s", err)
        return
    names = list(voices)
    speakers = [Speaker.AGENT if n == "lana" else Speaker.CLINIC for n in names]
    started = time.perf_counter()
    try:
        results = await warm_up(list(zip(voices.values(), speakers)))
    except Exception as err:  # ElevenLabs being down must never stop the server
        log.warning("TTS warm-up failed: %s: %s", type(err).__name__, err)
        return
    summary = ", ".join(f"{n} {'ok' if ok else 'failed'}" for n, ok in zip(names, results))
    log.info("TTS warm-up took %.1fs: %s", time.perf_counter() - started, summary)


def _demo_patient_lines() -> list[str]:
    lines = [DEMO_DONE_LINE]
    for patient in load_demo_patients():
        lines += [patient.request, *patient.answers.values()]
    return list(dict.fromkeys(lines))


async def _prewarm_lines() -> None:
    """Cache the fixed lines in TTS, so they play at once and cost credits only once.

    Lana's patient lines and call openings, then the demo patient's lines.
    """
    if not get_settings().elevenlabs_api_key.get_secret_value():
        return
    started = time.perf_counter()
    intake_lines = Intake()._lines
    lana = [
        _greeting(), *FIXED_LINES,
        *(intake_lines[topic] for topic in TOPIC_FIELDS),
        *(intake_lines[f"again_{topic}"] for topic in TOPIC_FIELDS),
        *(intake_lines[f"default_{topic}"] for topic in TOPIC_FIELDS),
        *opening_lines(),
    ]
    for speaker, lines in ((Speaker.AGENT, lana), (Speaker.PATIENT, _demo_patient_lines())):
        try:
            voice_id = voice_for(speaker)
        except VoiceConfigError as err:
            log.warning("TTS prewarm skipped for %s: %s", speaker.value, err)
            continue
        try:
            results = await tts.prewarm([(text, voice_id) for text in lines], speaker)
        except Exception as err:  # ElevenLabs being down must never stop the server
            log.warning("TTS prewarm failed: %s: %s", type(err).__name__, err)
            return
        summary = ", ".join(f"{r} {results.count(r)}" for r in sorted(set(results)))
        log.info("TTS prewarm (%s): %s", speaker.value, summary)
    # On Railway the disk is temporary, so this rebuilds the cache after every deploy.
    log.info("TTS prewarm took %.1fs", time.perf_counter() - started)


async def _warm_start() -> None:
    """Voice warm-up, then the prewarm. Runs in the background so startup never waits on ElevenLabs."""
    await _warm_up_voices()
    await _prewarm_lines()


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    missing = settings.missing_keys()
    if missing:
        log.error("Missing %s: set them in the environment. /health reports ok: false until then.",
                  ", ".join(missing))
    if not settings.meta_base_url:
        log.warning("META_BASE_URL is blank; cases will fail at the first AI call")
    _open_clients()
    warm = asyncio.create_task(_warm_start())
    yield
    await _cancel(warm)


def cors_options(settings) -> dict:
    """CORSMiddleware options: WEB_ORIGIN's list, plus WEB_ORIGIN_REGEX if set."""
    return {
        "allow_origins": settings.web_origins,
        "allow_origin_regex": settings.web_origin_regex or None,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


app = FastAPI(title="Patient Agent", lifespan=lifespan)
app.add_middleware(CORSMiddleware, **cors_options(get_settings()))


@app.exception_handler(Exception)
async def server_error(request: Request, err: Exception) -> JSONResponse:
    log.exception("%s %s failed", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"code": "server_error", "message": SERVER_ERROR_MESSAGE})


@app.get("/health")
async def health() -> dict:
    missing = get_settings().missing_keys()
    return {"ok": not missing, "missing": missing}


def _clock_time(slot) -> str:
    """'Tue 5:30 PM' (no %-I: Windows has no such format)."""
    hour = slot.hour % 12 or 12
    return f"{slot:%a} {hour}:{slot:%M} {'AM' if slot.hour < 12 else 'PM'}"


@app.get("/config/judge-sheet")
async def judge_sheet() -> dict:
    """The facts on the judge's script card in Be the clinic (Parkway's sheet, which the judge plays).

    Only the judge's card reads this; Lana never sees a price sheet.
    """
    clinic, sheet = next((c, s) for c, s in load_demo_clinics() if c.id == JUDGE_CLINIC_ID)
    return {
        "clinic_name": clinic.name,
        "treatment": sheet.list_price,
        "records_fee": sheet.records_fee,
        "monthly": sheet.monthly,
        "down_payment": sheet.down_payment,
        "evening_openings": [_clock_time(s) for s in sheet.open_slots if s.hour >= 17],
        "lowest_price": sheet.floor_price,
        "can_match_price": sheet.will_price_match,
        "can_waive_records": sheet.will_waive_records,
        "pay_in_full_discount_pct": sheet.pay_in_full_discount_pct,
        "can_add_retainers": sheet.will_add_retainers,
    }


class CreateCase(BaseModel):
    mode: Literal["watch", "patient", "clinic"]
    voice: bool = True
    clinics: Literal[2, 3] = 2


@app.post("/cases")
async def create_case(body: CreateCase, request: Request):
    ip = guard.client_ip(request.headers.get("x-forwarded-for"), request.client.host if request.client else None)
    if not guard.allow_new_case(ip):
        return JSONResponse(status_code=429, content={"code": "limit", "message": guard.LIMIT_MESSAGE})
    clinics: int = body.clinics
    if body.mode == "clinic":
        # Two calls: Parkway (the judge) and a simulated Sunrise.
        case = Case(preferences=clinic_patient().preferences, mode=DemoMode.BE_THE_CLINIC)
        clinics = 2
    else:
        # A patient case has no preferences until intake fills them.
        case = Case(preferences=DEMO_PREFERENCES if body.mode == "watch" else None, mode=DemoMode.WATCH)
    store.create(case, voice=body.voice, clinics=clinics)
    log.info("case=%s created (%s, voice %s, %d clinics)", case.id, body.mode, body.voice, clinics)
    return {"case_id": case.id}


# ---------- WebSocket ----------


class Start(BaseModel):
    type: Literal["start"]


class Listen(BaseModel):
    """Start the patient's voice intake (or Be the clinic's briefing): Lana talks, the mic is transcribed."""

    type: Literal["listen"]


class UserText(BaseModel):
    type: Literal["user_text"]
    text: str


class Command(BaseModel):
    type: Literal["command"]
    name: str


class Answer(BaseModel):
    """The browser picked up Lana's callback (after 3 s of ringing, or a tap)."""

    type: Literal["answer"]


class Stop(BaseModel):
    type: Literal["stop"]


class BargeIn(BaseModel):
    """The patient started talking over Lana: the browser already stopped her audio."""

    type: Literal["barge_in"]


class NeedAudio(BaseModel):
    """Sound turned on in a case that started without it: voice these lines, and every line from now on.

    line_seqs: the line playing in the browser and those waiting in its buffer. after: the last
    seq the browser had, so lines already on their way are voiced too.
    """

    type: Literal["need_audio"]
    line_seqs: list[int] = []
    after: int = 0


ClientMessage = TypeAdapter(
    Annotated[Start | Listen | UserText | Command | Answer | Stop | BargeIn | NeedAudio, Field(discriminator="type")]
)


async def _run_and_ring(orchestrator: Orchestrator) -> None:
    """The calls, then Lana's callback: OFFERS_READY -> CALLBACK with her summary."""
    case = await orchestrator.run()
    if case.status == CaseStatus.OFFERS_READY:
        await orchestrator.start_callback()


async def run_case(
    record: CaseRecord,
    bus: AudioBus,
    orchestrator: Orchestrator,
    callback: bool = True,
    usage: guard.CaseUsage | None = None,
) -> None:
    """Run the case. A usage limit cancels the orchestrator and sends one limit error.

    Be the clinic has no callback (callback=False): the run ends at OFFERS_READY.
    """
    usage = usage or guard.CaseUsage(record.case.id)
    with guard.case_usage(usage):
        inner = asyncio.create_task(_run_and_ring(orchestrator) if callback else orchestrator.run())
    usage.on_limit = inner.cancel
    try:
        await inner
    except asyncio.CancelledError:
        if usage.hit is None or not inner.cancelled():
            raise
    except Exception:
        log.exception("case=%s run failed", record.case.id)
        await bus.send_payload(SERVER_ERROR)
        return
    if usage.hit is not None:
        await bus.send_payload(LIMIT_ERROR)
    log.info("case=%s run ended: %s (llm calls %d, tts chars %d)",
             record.case.id, record.case.status.value, usage.llm_calls, usage.tts_chars)


async def demo_intake(record: CaseRecord, bus: AudioBus) -> Preferences:
    """Watch mode's opening: a demo patient asks, Lana answers with her real intake.

    One patient line and Lana's reply; if she still needs something, the patient answers it
    and Lana confirms with defaults for the rest. If intake fails, the scenario's own
    preferences are used and Lana says a short confirmation.
    """
    case_id = record.case.id
    patient = random.choice(load_demo_patients())
    log.info("case=%s demo patient %s: %r", case_id, patient.id, patient.request)
    intake = Intake()
    await bus.patient_line(UserSaid(case_id=case_id, text=patient.request))
    try:
        turn = await intake.hear(patient.request)
        await bus.emit(AgentSaid(case_id=case_id, text=turn.spoken))
        if not turn.done:
            answer = patient.answers.get(intake.current or "", DEMO_DONE_LINE)
            await bus.patient_line(UserSaid(case_id=case_id, text=answer))
            turn = await intake.hear(answer, finish=True)
            await bus.emit(AgentSaid(case_id=case_id, text=turn.spoken))
        return intake.to_preferences()
    except guard.LimitHit:
        raise
    except Exception as err:
        log.warning("case=%s demo intake failed, using %s's preferences: %s: %s",
                    case_id, patient.id, type(err).__name__, err)
        await bus.emit(AgentSaid(case_id=case_id, text=DEMO_FALLBACK_CONFIRM))
        return patient.preferences


# ---------- the patient's voice session ----------

MP3_BYTES_PER_S = 128_000 // 8  # tts.OUTPUT_FORMAT is mp3_44100_128
TEXT_WORDS_PER_S = 2.6  # how fast the browser reveals a line that has no audio (player.ts)
LINE_TAIL_S = 0.5  # the browser decodes a line before it plays it
MAX_BACKLOG_CHUNKS = 30  # Meta closes a stream more than 5 s behind; keep at most 3 s queued
PACE_SLACK_S = 0.05  # how late a mic frame may be before silence takes its place
IDLE_CLOSE_S = 3.0  # no mic frame for this long: close the STT session until one comes


class LineClock:
    """Estimates when Lana's current line ends in the browser, from what the bus sends.

    With call_lines (Be the clinic) the voiced call lines count too, Lana's and the simulated
    clinic's, so the judge's turn starts only once the browser has played everything before it.
    The judge's own lines have no audio and never count.
    """

    def __init__(self, call_lines: bool = False) -> None:
        self._call_lines = call_lines
        self._open = False  # her line's text went out and its last audio frame hasn't
        self._bytes = 0
        self._words = 0
        self._until = 0.0

    def _opens(self, payload: dict) -> bool:
        kind = payload.get("type")
        if kind == "agent_said":
            return True
        return (
            self._call_lines
            and kind == "transcript_final"
            and not payload.get("judge")
            and payload.get("speaker") in (Speaker.AGENT.value, Speaker.CLINIC.value)
        )

    def track(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "audio" and payload.get("cut"):
            self.interrupt()  # a line cut short by the patient ends now
            return
        if self._opens(payload):
            self._open, self._bytes, self._words = True, 0, len(payload.get("text", "").split())
        elif kind == "audio" and self._open and (self._call_lines or payload.get("speaker") == Speaker.AGENT.value):
            self._bytes += len(payload.get("chunk_b64", "")) * 3 // 4
            if payload.get("last"):
                seconds = self._bytes / MP3_BYTES_PER_S if self._bytes else self._words / TEXT_WORDS_PER_S
                # Lines play back to back, so a queued line starts when the one before it ends.
                self._until = max(self._until, time.monotonic()) + seconds + LINE_TAIL_S
                self._open = False

    def speaking(self) -> bool:
        return self._open or time.monotonic() < self._until

    def interrupt(self) -> None:
        """The patient talked over Lana: her line, and any queued after it, end now."""
        self._open = False
        self._until = 0.0


def _normalize(text: str) -> str:
    text = re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'"))
    text = re.sub(r"^(?:(?:hey|ok|okay|so|um|uh|lana)\s+)+", "", re.sub(r"\s+", " ", text).strip())
    return re.sub(r"(?:\s+(?:lana|thanks|thank you|then))+$", "", text)


def is_step_away(text: str) -> bool:
    """The patient says they will step away while the calls run: "I'll wait", "call me back"."""
    return _normalize(text) in STEP_AWAY_PHRASES


def is_ready(text: str) -> bool:
    """The judge is ready for Lana's call: "ready", "let's go", or a plain yes."""
    return _normalize(text) in READY_PHRASES or match_command(text) == SpokenCommand.YES_BOOK


# Be the clinic adds two: the briefing on Home, then the judge's call.
Phase = Literal["intake", "calls", "callback", "over", "briefing", "clinic"]


class PatientSession:
    """The patient's voice on one socket: intake, then step away during the calls, then the callback.

    In Be the clinic the same voice is the judge's: the briefing, then every committed turn
    goes to the clinic's JudgeLine (`judge`) instead of intake.

    Every task runs inside this session's CaseUsage, so intake LLM calls, Lana's TTS and the
    STT seconds all count against the case. A Meta session is open only while the patient may
    talk: it closes when Lana talks or thinks (Meta has no pause) and reopens on the next mic
    frame, so only real mic audio is sent and counted.
    """

    def __init__(
        self,
        record: CaseRecord,
        bus: AudioBus,
        clock: LineClock,
        start_run: Callable[[Preferences, bool], Awaitable[None]],
        step_away: Callable[[], Awaitable[None]],
        phase: Phase = "intake",
    ) -> None:
        self.record = record
        self.bus = bus
        self.clock = clock
        self._start_run = start_run
        self._step_away = step_away
        self.usage = guard.CaseUsage(record.case.id)
        self.intake = Intake(greeting=_greeting())
        self.phase: Phase = phase
        self.listening = phase != "over"  # mic audio goes to Meta; off after a step away or a failure
        self.thinking = False  # a turn is being handled; Lana's reply comes next
        self.orchestrator: Orchestrator | None = None  # set for the callback
        self.judge: JudgeLine | None = None  # Be the clinic: the call the judge is on
        self.on_hold = False  # Be the clinic: the judge's call is on hold
        self._unclear = 0  # callback answers that were neither yes nor not yet
        self._turn_heard = False  # the open Meta session heard a whole turn; it closes next
        self._audio: asyncio.Queue[bytes] = asyncio.Queue()
        self._listener: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._turns = asyncio.Lock()
        self._other_time = False  # the callback offered a slot outside the patient's preferred time
        self._turn_no = 0  # turns taken; the greeting belongs to turn 0
        self._cut_turn: int | None = None  # the turn whose lines the patient talked over
        # Finals heard in the grace period, joined into one turn when it ends.
        self._pending: list[str] = []
        self._commit: asyncio.Task | None = None

    def _spawn(self, coro: Coroutine) -> asyncio.Task:
        with guard.case_usage(self.usage):
            task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def begin(self) -> None:
        self._spawn(self._say(BRIEFING if self.phase == "briefing" else _greeting()))

    def muted(self) -> bool:
        # After a barge-in the patient has the turn, even while Lana's turn is still finishing.
        thinking = self.thinking and self._cut_turn != self._turn_no
        return thinking or self.clock.speaking()

    def barge_in(self) -> bool:
        """The patient talked over Lana (the browser stopped her audio). Not during the calls.

        The line in progress stops sending, the rest of her current turn is not said, and
        what the patient says next is a new turn. In Be the clinic the judge can talk over
        her on their own call, but not while on hold.
        """
        if self.phase in ("calls", "over") or (self.phase == "clinic" and not self.listening):
            return False
        log.info("case=%s barge-in during turn %d", self.record.case.id, self._turn_no)
        self._cut_turn = self._turn_no
        self.bus.cut()
        self.clock.interrupt()
        self.resume_listening()
        return True

    def audio(self, chunk: bytes) -> None:
        """One mic frame from the browser. Dropped while Lana has the turn; never sent to Meta."""
        if not self.listening or self.muted():
            return
        self._audio.put_nowait(chunk)
        if self._listener is None or self._listener.done():
            self._listener = self._spawn(self._listen())

    def typed(self, text: str) -> None:
        """Text from the fallback: tapped phrases and the text box take the same path as speech."""
        if text.strip():
            self._spawn(self._turn(text))

    def stop_listening(self) -> None:
        self.listening = False
        if self._listener is not None:
            self._listener.cancel()
        while not self._audio.empty():
            self._audio.get_nowait()

    def resume_listening(self) -> None:
        self.listening = True

    def hold(self, on: bool) -> None:
        """Be the clinic: the judge is on hold while Lana calls the other clinic; nothing they say counts."""
        self.on_hold = on
        if on:
            self.stop_listening()
            self._pending = []
        else:
            self.resume_listening()

    def call_over(self) -> None:
        """Be the clinic: the judge's call ended."""
        self.on_hold = False
        self.stop_listening()
        self._pending = []
        self.phase = "over"

    async def close(self) -> None:
        self.stop_listening()
        for task in list(self._tasks):
            await _cancel(task)

    async def _say(self, text: str) -> None:
        if self._cut_turn == self._turn_no:
            log.info("case=%s not said, the patient talked over this turn: %r", self.record.case.id, text)
            return
        await self.bus.emit(AgentSaid(case_id=self.record.case.id, text=text))

    # ----- listening -----

    async def _listen(self) -> None:
        """One Meta session per stretch of patient audio, until listening stops."""
        try:
            while self.listening:
                first = await self._audio.get()
                if self.muted():
                    continue
                self._turn_heard = False
                async with StreamingSTT() as stt:
                    reader = asyncio.create_task(self._read(stt))
                    pump = asyncio.create_task(self._pump(stt, first))
                    try:
                        done, _ = await asyncio.wait({reader, pump}, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                        if pump in done:
                            # Lana has the turn or the mic went quiet: let Meta flush, then close.
                            await stt.finish()
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(asyncio.shield(reader), 1.0)
                    finally:
                        for task in (reader, pump):
                            await _cancel(task)
        except STTUnavailable as err:
            log.warning("case=%s speech to text unavailable: %s", self.record.case.id, err)
            self.listening = False
            await self.bus.send_payload({"type": "stt_unavailable"})
        except guard.LimitHit:
            self.listening = False
            await self.bus.send_payload(LIMIT_ERROR)
        finally:
            log.info("case=%s stt session over (stt seconds %.1f)", self.record.case.id, self.usage.stt_seconds)

    async def _pump(self, stt: StreamingSTT, chunk: bytes) -> None:
        """Send Meta one chunk per CHUNK_MS until Lana has the turn or the mic goes quiet.

        Only mic frames are charged. Silence fills a gap when a frame is late, so Meta's
        stream stays real time; it is never charged.
        """
        silence = bytes(AUDIO_FORMAT.chunk_bytes(CHUNK_MS))
        sent_until = time.monotonic()  # wall time at which the audio sent so far runs out
        last_mic = time.monotonic()
        real = True
        while True:
            if self._turn_heard or self.muted() or not self.listening:
                return
            now = time.monotonic()
            if not real and now - last_mic > IDLE_CLOSE_S:
                return
            seconds = len(chunk) / AUDIO_FORMAT.bytes_per_second
            if real:
                guard.charge_stt(seconds)
                last_mic = now
            await stt.send_audio(chunk)
            now = time.monotonic()
            sent_until = (now if sent_until < now - 1.0 else sent_until) + seconds

            while self._audio.qsize() > MAX_BACKLOG_CHUNKS:
                self._audio.get_nowait()
            try:
                chunk, real = self._audio.get_nowait(), True
            except asyncio.QueueEmpty:
                wait = max(0.0, sent_until - time.monotonic()) + PACE_SLACK_S
                try:
                    chunk, real = await asyncio.wait_for(self._audio.get(), wait), True
                except TimeoutError:
                    chunk, real = silence, False

    async def _read(self, stt: StreamingSTT) -> None:
        async for event in stt:
            if isinstance(event, Partial):
                if not self.muted() and self.phase != "calls":
                    if self.judge is not None and self.phase == "clinic":
                        self.judge.talking()  # mid-sentence: not a silence
                    if self._pending:
                        # Still talking after a pause: hold the turn open a while longer.
                        self._arm_commit(get_settings().end_grace_ms / 1000 + GRACE_PARTIAL_EXTRA_S)
                    text = " ".join([*self._pending, event.text])
                    await self.bus.send_payload({"type": "user_partial", "text": text})
            elif isinstance(event, Final):
                if self.muted() or not self.listening:
                    log.info("case=%s heard %r while Lana had the turn; ignored", self.record.case.id, event.text)
                elif self.phase == "calls":
                    # During the calls the mic also hears the clinics; only stepping away counts.
                    if is_step_away(event.text):
                        self._spawn(self._step_away())
                    else:
                        log.info("case=%s heard %r during the calls; ignored", self.record.case.id, event.text)
                else:
                    # A pause may not be the end: wait END_GRACE_MS in case the patient goes on.
                    self._pending.append(event.text.strip())
                    self._arm_commit(get_settings().end_grace_ms / 1000)

    def _arm_commit(self, delay: float) -> None:
        if self._commit is not None and not self._commit.done():
            self._commit.cancel()
        self._commit = self._spawn(self._commit_after(delay))

    async def _commit_after(self, delay: float) -> None:
        """The grace period ended with no more speech: everything heard is one turn."""
        await asyncio.sleep(delay)
        text = " ".join(t for t in self._pending if t)
        self._pending = []
        if not text or not self.listening:
            return
        self.thinking = True  # before the task starts, so a second final can't slip in
        self._turn_heard = True
        self._spawn(self._turn(text))

    # ----- turns -----

    async def _turn(self, text: str) -> None:
        text = text.strip()
        async with self._turns:
            self._turn_no += 1
            self.thinking = True
            # No filler words while she thinks: the browser shows a quiet thinking state instead.
            work = asyncio.create_task(self._handle(text))
            try:
                await work
            except guard.LimitHit:
                self.stop_listening()
                await self.bus.send_payload(LIMIT_ERROR)
            except Exception as err:
                log.warning("case=%s turn failed for %r: %s: %s", self.record.case.id, text, type(err).__name__, err)
                with contextlib.suppress(Exception):
                    await self._say(SORRY)
            finally:
                await _cancel(work)
                self.thinking = False

    async def _handle(self, text: str) -> None:
        case_id = self.record.case.id
        if self.phase == "clinic":
            # The judge's words become the clinic's line on the call (shown by the orchestrator).
            if self.judge is not None:
                log.info("case=%s judge said %r", case_id, text)
                self.judge.hear(text)
            else:
                log.info("case=%s ignored %r: the judge's call hasn't started", case_id, text)
            return
        if self.phase == "calls":
            if is_step_away(text):
                await self._step_away()
            else:
                log.info("case=%s ignored %r: the calls are running", case_id, text)
            return
        if self.phase == "over":
            log.info("case=%s ignored %r: nothing is waiting on an answer", case_id, text)
            return
        await self.bus.emit(UserSaid(case_id=case_id, text=text))
        if self.phase == "callback":
            await self._booking_answer(text)
            return
        if self.phase == "briefing":
            await self._briefing_answer(text)
            return
        # While Lana waits on an answer to her question, short replies go straight to intake.
        command = await parse_command(text, classify=self.intake.current is None)
        log.info("case=%s heard %r -> %s", case_id, text, command or self.phase)
        if command == SpokenCommand.WATCH:
            self.stop_listening()
            self.phase = "over"
            await self._start_run(DEMO_PREFERENCES, False)
        elif command == SpokenCommand.CLINIC:
            # The browser starts a new Be the clinic case (POST /cases mode clinic).
            self.stop_listening()
            self.phase = "over"
            await self.bus.send_payload({"type": "command", "name": "clinic"})
        elif command == SpokenCommand.START_OVER:
            self.stop_listening()
            await self.bus.send_payload({"type": "command", "name": "start_over"})
        else:
            # skip, yes and no are answers to Lana's question here; Intake reads them.
            await self._answer(text)

    async def _answer(self, text: str) -> None:
        turn = await self.intake.hear(text)
        update: dict = {
            "type": "intake",
            "topic": self.intake.current,
            "done": turn.done,
            "draft": self.intake.draft.model_dump(mode="json", exclude_none=True),
        }
        if turn.done:
            self.record.case.preferences = self.intake.to_preferences()
            update["preferences"] = self.record.case.preferences.model_dump(mode="json")
        await self.bus.send_payload(update)
        await self._say(turn.spoken)
        if turn.done:
            # The calls start right away with the patient's answers; the mic stays open so
            # they can say "I'll wait".
            self.phase = "calls"
            await self._start_run(self.record.case.preferences, True)

    async def _briefing_answer(self, text: str) -> None:
        """Be the clinic's briefing: "ready" starts Lana's calls; the judge's mic stays open."""
        if is_ready(text):
            self.phase = "clinic"
            await self._start_run(self.record.case.preferences, True)
        elif match_command(text) == SpokenCommand.START_OVER:
            self.stop_listening()
            await self.bus.send_payload({"type": "command", "name": "start_over"})
        else:
            await self._say(READY_AGAIN)

    # ----- the callback -----

    async def callback(self, run: asyncio.Task, orchestrator: Orchestrator) -> None:
        """The browser answered Lana's call: wait for her summary, say it, ask to book."""
        if self.phase == "callback":
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(run)
        if orchestrator.case.status != CaseStatus.CALLBACK or orchestrator.summary is None:
            log.info("case=%s answered, but no callback is ready (%s)", self.record.case.id,
                     orchestrator.case.status.value)
            return
        self.orchestrator = orchestrator
        if not orchestrator.case.offers:
            # Nothing to book: Lana says what happened and the case ends saved.
            self.phase = "over"
            await self._say(orchestrator.summary)
            await orchestrator.save()
            return
        self.phase = "callback"
        self._unclear = 0
        self.resume_listening()
        top = orchestrator.case.offers[0]
        prefs = orchestrator.case.preferences
        time_pref = prefs.time_pref if prefs is not None else "any"
        slot, matches = pick_slot(top, time_pref)
        self._other_time = slot is not None and not matches
        if self._other_time:
            # Honest: no opening in the time they asked for. Booking another time needs their yes.
            question = NO_MATCH_QUESTION.format(times=TIME_PLURAL[time_pref], slot=spoken_slot(slot))
        else:
            question = BOOK_ANYWAY if top.over_budget_by > 0 else BOOK_QUESTION
        await self._say(f"{orchestrator.summary} {question}")

    async def _booking_answer(self, text: str) -> None:
        """Yes books the winner's slot in the patient's time (or the one Lana offered); not yet saves the quote.

        Nothing books without a clear yes.
        """
        orchestrator = self.orchestrator
        assert orchestrator is not None
        command = await parse_command(text, classify=True)
        log.info("case=%s heard %r -> %s (callback)", self.record.case.id, text, command)
        if command == SpokenCommand.START_OVER:
            self.stop_listening()
            await self.bus.send_payload({"type": "command", "name": "start_over"})
            return
        if command not in (SpokenCommand.YES_BOOK, SpokenCommand.NOT_YET):
            self._unclear += 1
            if self._unclear < 2:
                await self._say(BOOK_AGAIN)
                return
            command = SpokenCommand.NOT_YET  # asked twice: keep the quote, book nothing

        self.phase = "over"
        self.stop_listening()
        top = orchestrator.case.offers[0] if orchestrator.case.offers else None
        prefs = orchestrator.case.preferences
        time_pref = prefs.time_pref if prefs is not None else "any"
        slot = pick_slot(top, time_pref)[0] if top is not None else None
        # Lana's line plays before the outcome event, so Done appears once she has said it.
        if command == SpokenCommand.YES_BOOK and top is not None and slot is not None:
            await self._say(BOOKED_LINE.format(clinic=top.clinic.name, slot=spoken_slot(slot)))
            # The yes was to this slot, so a slot outside their time books too.
            await orchestrator.book(any_time=self._other_time)
        else:
            await self._say(NO_SLOT_LINE if command == SpokenCommand.YES_BOOK else SAVED_LINE)
            await orchestrator.save()


# ---------- the socket ----------


def origin_allowed(origin: str | None, settings) -> bool:
    """CORS's rules for the socket (CORS doesn't cover WebSockets): WEB_ORIGIN's list or WEB_ORIGIN_REGEX.

    No Origin header (CLI tools like agent ws-watch; browsers always send one) is allowed.
    """
    if origin is None:
        return True
    if "*" in settings.web_origins or origin in settings.web_origins:
        return True
    return bool(settings.web_origin_regex) and re.fullmatch(settings.web_origin_regex, origin) is not None


@app.websocket("/ws/case/{case_id}")
async def case_socket(websocket: WebSocket, case_id: str) -> None:
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, get_settings()):
        log.warning("case=%s socket refused: origin %r is not allowed", case_id, origin)
        await websocket.close(code=1008)
        return
    await websocket.accept()
    record = store.get(case_id)
    if record is None:
        await websocket.send_json(
            {"type": "error", "code": "not_found", "message": "No such case, or it expired.", "case_id": case_id}
        )
        await websocket.close(code=4404)
        return

    clinic_case = record.case.mode == DemoMode.BE_THE_CLINIC
    clock = LineClock(call_lines=clinic_case)

    async def send(payload: dict) -> None:
        clock.track(payload)
        await websocket.send_json(payload)

    bus = AudioBus(record, send)
    run: asyncio.Task | None = None
    intro: asyncio.Task | None = None  # Watch mode's demo patient and Lana, before the calls
    orchestrator: Orchestrator | None = None
    session: PatientSession | None = None
    voice = record.voice  # the case started with sound; need_audio turns it on mid-run
    away = False  # stepped away: call audio is off for a while
    run_usage: guard.CaseUsage | None = None  # the run's counters, so late audio is charged to it
    intro_usage: guard.CaseUsage | None = None  # Watch mode's opening, before the run
    late_audio: set[asyncio.Task] = set()

    async def start_run(preferences: Preferences, keep_listening: bool = False) -> None:
        """The calls: from the start message, "just show me", or the end of the patient's intake."""
        nonlocal run, orchestrator, run_usage
        if run is not None and not run.done():
            await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
        elif not guard.allow_run(record.runs):
            await bus.send_payload(LIMIT_ERROR)
        else:
            if session is not None and not keep_listening:
                session.stop_listening()
            record.runs += 1
            record.case = Case(id=record.case.id, preferences=preferences, mode=record.case.mode)
            run_usage = guard.CaseUsage(record.case.id)
            if clinic_case:
                orchestrator = Orchestrator(
                    record.case, clinic_emit, max_clinics=record.clinics, receptionists=receptionists
                )
                run = asyncio.create_task(run_case(record, bus, orchestrator, callback=False, usage=run_usage))
                if session is not None:
                    # Out of STT seconds: nobody can hear the judge, so the calls stop too.
                    session.usage.on_limit = run.cancel
            else:
                orchestrator = Orchestrator(record.case, bus.emit, max_clinics=record.clinics)
                run = asyncio.create_task(run_case(record, bus, orchestrator, usage=run_usage))

    # ----- Be the clinic -----

    judge_calls: set[str] = set()  # call ids of the judge's clinic

    async def lana_done() -> None:
        """Until Lana's line (and every call line before it) has played in the browser."""
        while clock.speaking():
            await asyncio.sleep(0.05)

    def receptionists(clinic: Clinic, sheet: PriceSheet, say: Say):
        """The judge plays JUDGE_CLINIC_ID; every other clinic is simulated."""
        if clinic.id != JUDGE_CLINIC_ID:
            return simulated(clinic, sheet, say)

        async def turn_start(phase: JudgePhase) -> None:
            await bus.emit(JudgeTurnStart(case_id=record.case.id, clinic_id=clinic.id, phase=phase))

        judge = JudgeLine(clinic, say=say, turn_start=turn_start, lana_done=lana_done)
        if session is not None:
            session.judge = judge
        return judge

    async def clinic_emit(event) -> None:
        """The orchestrator's emit in Be the clinic. The judge's lines go out unvoiced, and the
        judge's call status drives the session (listening, hold) and the browser's card."""
        if isinstance(event, CallStatusEvent) and event.clinic_id == JUDGE_CLINIC_ID:
            judge_calls.add(event.call_id)
            await bus.emit(event)
            if session is None:
                return
            if event.status == CallStatus.DIALING:
                session.phase = "clinic"
                session.resume_listening()
            elif event.status == CallStatus.ON_HOLD:
                session.hold(True)
                await bus.emit(JudgeHold(case_id=record.case.id, clinic_id=event.clinic_id, on_hold=True))
            elif event.status == CallStatus.LIVE and session.on_hold:
                session.hold(False)
                await bus.emit(JudgeHold(case_id=record.case.id, clinic_id=event.clinic_id, on_hold=False))
            elif event.status == CallStatus.ENDED:
                session.call_over()
                # Only a hint for the card ("Thanks, bye!"): nobody waits for it.
                await bus.emit(JudgeTurnStart(case_id=record.case.id, clinic_id=event.clinic_id, phase="wrap"))
            return
        if isinstance(event, TranscriptFinal) and event.call_id in judge_calls and event.speaker == Speaker.CLINIC:
            if not guard.limit_hit():
                await bus.send_payload(event.model_copy(update={"judge": True}).model_dump(mode="json"))
            return
        await bus.emit(event)

    def set_away(now_away: bool) -> None:
        # While stepped away, call lines go out as text only: no TTS spent, and the calls run faster.
        nonlocal away
        away = now_away
        record.voice = voice and not away
        if away and orchestrator is not None and run is not None and not run.done():
            orchestrator.stepped_away = True  # her callback says how many calls she made meanwhile
        if session is not None and session.phase == "calls":
            if away:
                session.stop_listening()
            else:
                session.resume_listening()

    async def need_audio(seqs: list[int], after: int) -> None:
        """Sound turned on mid-run: every line from now on is voiced, and the ones already sent get audio."""
        nonlocal voice
        voice = True
        record.voice = not away
        if run is not None and not run.done() and run_usage is not None:
            usage = run_usage
        elif session is not None:
            usage = session.usage
        else:
            usage = intro_usage or guard.CaseUsage(record.case.id)
        with guard.case_usage(usage):
            await bus.voice_late(seqs, after)

    async def step_away() -> None:
        """The patient said "I'll wait": the browser dims to the calm screen."""
        set_away(True)
        await bus.send_payload({"type": "command", "name": "step_away"})

    def new_session(phase: Phase = "intake") -> PatientSession:
        if phase == "intake" and clinic_case:
            phase = "briefing"
        return PatientSession(record, bus, clock, start_run, step_away, phase=phase)

    async def watch() -> None:
        """The start message: the demo patient's request and Lana's reply, then the calls."""
        nonlocal intro_usage
        usage = intro_usage = guard.CaseUsage(record.case.id)
        try:
            with guard.case_usage(usage):
                preferences = await demo_intake(record, bus)
        except guard.LimitHit:
            await bus.send_payload(LIMIT_ERROR)
            return
        await start_run(preferences)

    def busy() -> bool:
        return (run is not None and not run.done()) or (intro is not None and not intro.done())

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if session is not None:
                    session.audio(message["bytes"])
                continue
            try:
                msg = ClientMessage.validate_json(message.get("text") or "")
            except ValidationError:
                await bus.send_payload(
                    {"type": "error", "code": "bad_message",
                     "message": "Send start, listen, user_text, command, answer, need_audio or stop as JSON."}
                )
                continue

            if isinstance(msg, Start) and clinic_case:
                await bus.send_payload(
                    {"type": "error", "code": "bad_message", "message": "A clinic case opens with listen."}
                )
            elif isinstance(msg, Start):
                if busy():
                    await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
                elif not guard.allow_run(record.runs):
                    await bus.send_payload(LIMIT_ERROR)
                else:
                    intro = asyncio.create_task(watch())
            elif isinstance(msg, Listen):
                if session is not None or busy():
                    await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
                else:
                    session = new_session()
                    session.begin()
            elif isinstance(msg, UserText) and session is not None:
                session.typed(msg.text)
            elif isinstance(msg, Answer):
                if run is None or orchestrator is None:
                    await bus.send_payload({"type": "error", "code": "no_callback", "message": "There is no call to answer."})
                else:
                    set_away(False)  # Lana's callback is always voiced
                    if session is None:
                        session = new_session("over")  # Watch: a session just for the booking answer
                    session._spawn(session.callback(run, orchestrator))
            elif isinstance(msg, BargeIn):
                if session is None or not session.barge_in():
                    log.info("case=%s barge-in ignored: Lana isn't talking to the patient", case_id)
            elif isinstance(msg, NeedAudio):
                task = asyncio.create_task(need_audio(msg.line_seqs, msg.after))
                late_audio.add(task)
                task.add_done_callback(late_audio.discard)
            elif isinstance(msg, Command) and msg.name in ("step_away", "step_back"):
                set_away(msg.name == "step_away")
            elif isinstance(msg, Stop):
                if session is not None:
                    session.stop_listening()
                await _cancel(intro)
                await _cancel(run)
            else:
                # Spoken commands arrive as text (user_text) and are parsed; later modes plug in here.
                log.info("case=%s ignored %s", case_id, msg.type)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("case=%s socket failed", case_id)
        with contextlib.suppress(Exception):
            await websocket.send_json(SERVER_ERROR)
    finally:
        if session is not None:
            await session.close()
        await _cancel(intro)
        await _cancel(run)
        for task in list(late_audio):
            await _cancel(task)
        log.info("case=%s socket closed", case_id)
