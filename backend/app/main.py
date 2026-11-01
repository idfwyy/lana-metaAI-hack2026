"""FastAPI app: REST routes and the /ws/case/{id} WebSocket that streams a case to the browser."""

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app import guard
from app.agent.orchestrator import Orchestrator
from app.cli import DEMO_PREFERENCES
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.db.store import CaseRecord, store
from app.integrations import meta_llm
from app.schemas.case import Case
from app.schemas.enums import DemoMode, Speaker
from app.voice import tts
from app.voice.audio_bus import AudioBus
from app.voice.tts import VoiceConfigError, check_voices, warm_up

log = logging.getLogger(__name__)
# uvicorn configures only its own loggers; this shows the app's INFO lines (warm-up, runs, limits).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

LIMIT_ERROR = {"type": "error", "code": "limit", "message": guard.LIMIT_MESSAGE}


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.meta_api_key.get_secret_value() or not settings.meta_base_url:
        log.warning("META_API_KEY or META_BASE_URL is blank; cases will fail at the first AI call")
    _open_clients()
    await _warm_up_voices()
    yield


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
    if body.mode != "watch":
        return JSONResponse(
            status_code=501,
            content={"detail": f"Mode '{body.mode}' is not built yet. Only watch runs in this build."},
        )
    ip = request.client.host if request.client else "unknown"
    if not guard.allow_new_case(ip):
        return JSONResponse(status_code=429, content={"code": "limit", "message": guard.LIMIT_MESSAGE})
    case = Case(preferences=DEMO_PREFERENCES, mode=DemoMode.WATCH)
    store.create(case, voice=body.voice, clinics=body.clinics)
    log.info("case=%s created (watch, voice %s, %d clinics)", case.id, body.voice, body.clinics)
    return {"case_id": case.id}


# ---------- WebSocket ----------


class Start(BaseModel):
    type: Literal["start"]


class UserText(BaseModel):
    type: Literal["user_text"]
    text: str


class Command(BaseModel):
    type: Literal["command"]
    name: str


class Stop(BaseModel):
    type: Literal["stop"]


ClientMessage = TypeAdapter(Annotated[Start | UserText | Command | Stop, Field(discriminator="type")])


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


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


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

    bus = AudioBus(record, websocket.send_json)
    run: asyncio.Task | None = None
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                # TODO(Part 12a): mic audio frames go to StreamingSTT here.
                continue
            try:
                msg = ClientMessage.validate_json(message.get("text") or "")
            except ValidationError:
                await bus.send_payload(
                    {"type": "error", "code": "bad_message",
                     "message": "Send start, user_text, command or stop as JSON."}
                )
                continue

            if isinstance(msg, Start):
                if run is not None and not run.done():
                    await bus.send_payload({"type": "error", "code": "running", "message": "The case is already running."})
                elif not guard.allow_run(record.runs):
                    await bus.send_payload(LIMIT_ERROR)
                else:
                    record.runs += 1
                    record.case = Case(id=record.case.id, preferences=DEMO_PREFERENCES, mode=DemoMode.WATCH)
                    run = asyncio.create_task(run_watch(record, bus))
            elif isinstance(msg, Stop):
                await _cancel(run)
            else:
                # Watch mode reads neither yet; later modes plug in here.
                log.info("case=%s ignored %s in watch mode", case_id, msg.type)
    except WebSocketDisconnect:
        pass
    finally:
        await _cancel(run)
        log.info("case=%s socket closed", case_id)
