"""FastAPI app: REST routes and the /ws/case/{id} WebSocket that streams a case to the browser.

On the same socket the browser can send mic audio as binary frames (AUDIO_FORMAT, 100 ms
each). A PatientSession turns them into transcripts, then into commands or intake answers,
and Lana speaks the reply. Half duplex: the browser mutes the mic while Lana talks, and the
server sends silence to Meta in place of any audio that arrives during her lines.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app import guard
from app.agent.commands import Command as SpokenCommand
from app.agent.commands import parse_command
from app.agent.intake import TOPIC_FIELDS, Intake
from app.agent.orchestrator import Orchestrator
from app.cli import DEMO_PREFERENCES
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.db.store import CaseRecord, store
from app.integrations import meta_llm
from app.schemas.case import Case
from app.schemas.enums import DemoMode, Speaker
from app.schemas.events import AgentSaid, UserSaid
from app.voice import tts
from app.voice.audio_bus import AudioBus
from app.voice.stt import AUDIO_FORMAT, CHUNK_MS, Final, Partial, StreamingSTT, STTUnavailable
from app.voice.tts import VoiceConfigError, check_voices, voice_for, warm_up

log = logging.getLogger(__name__)
# uvicorn configures only its own loggers; this shows the app's INFO lines (warm-up, runs, limits).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

LIMIT_ERROR = {"type": "error", "code": "limit", "message": guard.LIMIT_MESSAGE}

# Lana's fixed lines to the patient. The full disclaimer stays in "About this demo".
GREETING = (
    "Hey, I'm {agent_name}, your personal health companion! Quick heads-up, this is a live demo, "
    "so I'll call demo clinics, not real offices. What do you need help with?"
)
COMING_SOON = "That mode is coming soon."
SORRY = "Sorry, I missed that. Could you say it again?"


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


async def _prewarm_lines() -> None:
    """Cache Lana's fixed patient lines in TTS, so they play at once and cost credits only once."""
    if not get_settings().elevenlabs_api_key.get_secret_value():
        return
    try:
        voice_id = voice_for(Speaker.AGENT)
    except VoiceConfigError as err:
        log.warning("TTS prewarm skipped: %s", err)
        return
    questions = Intake()._lines
    lines = [_greeting(), COMING_SOON, SORRY, *(questions[topic] for topic in TOPIC_FIELDS)]
    try:
        results = await tts.prewarm([(text, voice_id) for text in lines])
    except Exception as err:  # ElevenLabs being down must never stop the server
        log.warning("TTS prewarm failed: %s: %s", type(err).__name__, err)
        return
    log.info("TTS prewarm: %s", ", ".join(f"{r} {results.count(r)}" for r in sorted(set(results))))


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.meta_api_key.get_secret_value() or not settings.meta_base_url:
        log.warning("META_API_KEY or META_BASE_URL is blank; cases will fail at the first AI call")
    _open_clients()
    await _warm_up_voices()
    prewarm = asyncio.create_task(_prewarm_lines())
    yield
    await _cancel(prewarm)


app = FastAPI(title="Patient Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


class CreateCase(BaseModel):
    mode: Literal["watch", "patient", "clinic"]
    voice: bool = True
    clinics: Literal[2, 3] = 2


@app.post("/cases")
async def create_case(body: CreateCase, request: Request):
    if body.mode == "clinic":
        return JSONResponse(
            status_code=501,
            content={"detail": f"Mode '{body.mode}' is not built yet. Watch and patient run in this build."},
        )
    ip = request.client.host if request.client else "unknown"
    if not guard.allow_new_case(ip):
        return JSONResponse(status_code=429, content={"code": "limit", "message": guard.LIMIT_MESSAGE})
    # A patient case has no preferences until intake fills them.
    preferences = DEMO_PREFERENCES if body.mode == "watch" else None
    case = Case(preferences=preferences, mode=DemoMode.WATCH)
    store.create(case, voice=body.voice, clinics=body.clinics)
    log.info("case=%s created (%s, voice %s, %d clinics)", case.id, body.mode, body.voice, body.clinics)
    return {"case_id": case.id}


# ---------- WebSocket ----------


class Start(BaseModel):
    type: Literal["start"]


class Listen(BaseModel):
    """Start the patient's voice intake: Lana greets them and the mic audio is transcribed."""

    type: Literal["listen"]


class UserText(BaseModel):
    type: Literal["user_text"]
    text: str


class Command(BaseModel):
    type: Literal["command"]
    name: str


class Stop(BaseModel):
    type: Literal["stop"]


ClientMessage = TypeAdapter(Annotated[Start | Listen | UserText | Command | Stop, Field(discriminator="type")])


async def run_watch(record: CaseRecord, bus: AudioBus) -> None:
    """Run the case. A usage limit cancels the orchestrator and sends one limit error."""
    usage = guard.CaseUsage(record.case.id)
    with guard.case_usage(usage):
        inner = asyncio.create_task(
            Orchestrator(record.case, bus.emit, max_clinics=record.clinics).run()
        )
    usage.on_limit = inner.cancel
    try:
        await inner
    except asyncio.CancelledError:
        if usage.hit is None or not inner.cancelled():
            raise
    if usage.hit is not None:
        await bus.send_payload(LIMIT_ERROR)
    log.info("case=%s run ended: %s (llm calls %d, tts chars %d)",
             record.case.id, record.case.status.value, usage.llm_calls, usage.tts_chars)


# ---------- the patient's voice session ----------

MP3_BYTES_PER_S = 128_000 // 8  # tts.OUTPUT_FORMAT is mp3_44100_128
TEXT_WORDS_PER_S = 2.6  # how fast the browser reveals a line that has no audio (player.ts)
LINE_TAIL_S = 0.5  # the browser decodes a line before it plays it
MAX_BACKLOG_CHUNKS = 30  # Meta closes a stream more than 5 s behind; keep at most 3 s queued
PACE_SLACK_S = 0.05  # how late a mic frame may be before silence takes its place


class LineClock:
    """Estimates when Lana's current line ends in the browser, from what the bus sends."""

    def __init__(self) -> None:
        self._open = False  # her line's text went out and its last audio frame hasn't
        self._bytes = 0
        self._words = 0
        self._until = 0.0

    def track(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "agent_said":
            self._open, self._bytes, self._words = True, 0, len(payload.get("text", "").split())
        elif kind == "audio" and self._open and payload.get("speaker") == Speaker.AGENT.value:
            self._bytes += len(payload.get("chunk_b64", "")) * 3 // 4
            if payload.get("last"):
                seconds = self._bytes / MP3_BYTES_PER_S if self._bytes else self._words / TEXT_WORDS_PER_S
                # Lines play back to back, so a queued line starts when the one before it ends.
                self._until = max(self._until, time.monotonic()) + seconds + LINE_TAIL_S
                self._open = False

    def speaking(self) -> bool:
        return self._open or time.monotonic() < self._until


class PatientSession:
    """Voice intake on one socket: mic audio to StreamingSTT, each finished turn to a command or Intake.

    Every task runs inside this session's CaseUsage, so intake LLM calls, Lana's TTS and the
    STT seconds all count against the case. Audio waits while Lana talks or thinks: the pump
    sends Meta silence instead, which also keeps Meta's stream fed in real time.
    """

    def __init__(
        self, record: CaseRecord, bus: AudioBus, clock: LineClock, start_run: Callable[[], Awaitable[None]]
    ) -> None:
        self.record = record
        self.bus = bus
        self.clock = clock
        self._start_run = start_run
        self.usage = guard.CaseUsage(record.case.id)
        self.intake = Intake()
        self.phase: Literal["intake", "ready"] = "intake"
        self.thinking = False  # a turn is being handled; Lana's reply comes next
        self._stopped = False  # listening is over: Watch started, start over, STT failed or a limit
        self._audio: asyncio.Queue[bytes] = asyncio.Queue()
        self._listener: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._turns = asyncio.Lock()

    def _spawn(self, coro: Coroutine) -> asyncio.Task:
        with guard.case_usage(self.usage):
            task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def begin(self) -> None:
        self._spawn(self._say(_greeting()))

    def muted(self) -> bool:
        return self.thinking or self.clock.speaking()

    def audio(self, chunk: bytes) -> None:
        """One mic frame from the browser. The first one opens the STT stream."""
        if self._stopped:
            return
        if self._listener is None:
            self._listener = self._spawn(self._listen())
        self._audio.put_nowait(chunk)

    def typed(self, text: str) -> None:
        """Text from the fallback: tapped phrases and the text box take the same path as speech."""
        if text.strip():
            self._spawn(self._turn(text))

    def stop_listening(self) -> None:
        self._stopped = True
        if self._listener is not None:
            self._listener.cancel()

    async def close(self) -> None:
        self.stop_listening()
        for task in list(self._tasks):
            await _cancel(task)

    async def _say(self, text: str) -> None:
        await self.bus.emit(AgentSaid(case_id=self.record.case.id, text=text))

    # ----- listening -----

    async def _listen(self) -> None:
        try:
            async with StreamingSTT() as stt:
                reader = asyncio.create_task(self._read(stt))
                pump = asyncio.create_task(self._pump(stt))
                try:
                    done, _ = await asyncio.wait({reader, pump}, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in (reader, pump):
                        await _cancel(task)
        except STTUnavailable as err:
            log.warning("case=%s speech to text unavailable: %s", self.record.case.id, err)
            self._stopped = True
            await self.bus.send_payload({"type": "stt_unavailable"})
        except guard.LimitHit:
            self._stopped = True
            await self.bus.send_payload(LIMIT_ERROR)
        finally:
            log.info("case=%s stopped listening (stt seconds %.1f)", self.record.case.id, self.usage.stt_seconds)

    async def _pump(self, stt: StreamingSTT) -> None:
        """Send Meta one chunk per CHUNK_MS: the mic's, or silence while muted or when none came."""
        silence = bytes(AUDIO_FORMAT.chunk_bytes(CHUNK_MS))
        sent_until = time.monotonic()  # wall time at which the audio sent so far runs out
        while True:
            while self._audio.qsize() > MAX_BACKLOG_CHUNKS:
                self._audio.get_nowait()
            try:
                chunk = self._audio.get_nowait()
            except asyncio.QueueEmpty:
                wait = max(0.0, sent_until - time.monotonic()) + PACE_SLACK_S
                try:
                    chunk = await asyncio.wait_for(self._audio.get(), wait)
                except TimeoutError:
                    chunk = silence
            if self.muted():
                chunk = bytes(len(chunk))  # half duplex: Lana never hears herself
            seconds = len(chunk) / AUDIO_FORMAT.bytes_per_second
            guard.charge_stt(seconds)
            await stt.send_audio(chunk)
            now = time.monotonic()
            sent_until = (now if sent_until < now - 1.0 else sent_until) + seconds

    async def _read(self, stt: StreamingSTT) -> None:
        async for event in stt:
            if isinstance(event, Partial):
                if not self.muted():
                    await self.bus.send_payload({"type": "user_partial", "text": event.text})
            elif isinstance(event, Final):
                if self.muted() or self._stopped:
                    log.info("case=%s heard %r while Lana had the turn; ignored", self.record.case.id, event.text)
                    continue
                self.thinking = True  # before the task starts, so a second final can't slip in
                self._spawn(self._turn(event.text))

    # ----- turns -----

    async def _turn(self, text: str) -> None:
        text = text.strip()
        async with self._turns:
            self.thinking = True
            try:
                await self._handle(text)
            except guard.LimitHit:
                self.stop_listening()
                await self.bus.send_payload(LIMIT_ERROR)
            except Exception as err:
                log.warning("case=%s turn failed for %r: %s: %s", self.record.case.id, text, type(err).__name__, err)
                with contextlib.suppress(Exception):
                    await self._say(SORRY)
            finally:
                self.thinking = False

    async def _handle(self, text: str) -> None:
        case_id = self.record.case.id
        await self.bus.emit(UserSaid(case_id=case_id, text=text))
        # While Lana waits on an answer to her question, short replies go straight to intake.
        command = await parse_command(text, classify=self.intake.current is None)
        log.info("case=%s heard %r -> %s", case_id, text, command or self.phase)
        if command == SpokenCommand.WATCH:
            self.stop_listening()
            await self._start_run()
        elif command == SpokenCommand.CLINIC:
            # TODO(Part 13): start Be the clinic.
            await self._say(COMING_SOON)
        elif command == SpokenCommand.START_OVER:
            self.stop_listening()
            await self.bus.send_payload({"type": "command", "name": "start_over"})
        elif self.phase == "intake":
            # skip, yes and no are answers to Lana's question here; Intake reads them.
            await self._answer(text)
        else:
            # TODO(Part 12b): "yes" (SpokenCommand.YES_BOOK) starts the calls with record.case.preferences.
            log.info("case=%s ignored %r: intake is done", case_id, text)

    async def _answer(self, text: str) -> None:
        turn = await self.intake.hear(text)
        update: dict = {
            "type": "intake",
            "topic": self.intake.current,
            "done": turn.done,
            "draft": self.intake.draft.model_dump(mode="json", exclude_none=True),
        }
        if turn.done:
            self.phase = "ready"
            self.record.case.preferences = self.intake.to_preferences()
            update["preferences"] = self.record.case.preferences.model_dump(mode="json")
            # TODO(Part 12b): start the calls with record.case.preferences after the confirmation.
        await self.bus.send_payload(update)
        await self._say(turn.spoken)


# ---------- the socket ----------


@app.websocket("/ws/case/{case_id}")
async def case_socket(websocket: WebSocket, case_id: str) -> None:
    await websocket.accept()
    record = store.get(case_id)
    if record is None:
        await websocket.send_json(
            {"type": "error", "code": "not_found", "message": "No such case, or it expired.", "case_id": case_id}
        )
        await websocket.close(code=4404)
        return

    clock = LineClock()

    async def send(payload: dict) -> None:
        clock.track(payload)
        await websocket.send_json(payload)

    bus = AudioBus(record, send)
    run: asyncio.Task | None = None
    session: PatientSession | None = None

    async def start_run() -> None:
        """The Watch run, from the start message or from the patient saying "just show me"."""
        nonlocal run
        if run is not None and not run.done():
            await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
        elif not guard.allow_run(record.runs):
            await bus.send_payload(LIMIT_ERROR)
        else:
            if session is not None:
                session.stop_listening()
            record.runs += 1
            record.case = Case(id=record.case.id, preferences=DEMO_PREFERENCES, mode=DemoMode.WATCH)
            run = asyncio.create_task(run_watch(record, bus))

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
                     "message": "Send start, listen, user_text, command or stop as JSON."}
                )
                continue

            if isinstance(msg, Start):
                await start_run()
            elif isinstance(msg, Listen):
                if session is not None or (run is not None and not run.done()):
                    await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
                else:
                    session = PatientSession(record, bus, clock, start_run)
                    session.begin()
            elif isinstance(msg, UserText) and session is not None:
                session.typed(msg.text)
            elif isinstance(msg, Stop):
                if session is not None:
                    session.stop_listening()
                await _cancel(run)
            else:
                # Commands arrive as text (user_text) and are parsed; later modes plug in here.
                log.info("case=%s ignored %s", case_id, msg.type)
    except WebSocketDisconnect:
        pass
    finally:
        if session is not None:
            await session.close()
        await _cancel(run)
        log.info("case=%s socket closed", case_id)
