"""Muse Voice Transcribe: streaming speech to text over Meta's realtime WebSocket, plus file upload.

StreamingSTT opens the session in ENDPOINTING mode, so Meta decides when the speaker has
stopped. Our own 700 ms silence rule only steps in for a turn Meta never opened with
speechStart; a turn Meta did open gets a 2 s safety net instead, so we never race Meta's
endpointing. See docs/decisions.md for what the Meta docs confirm.

Callers send audio already in AUDIO_FORMAT; nothing here converts or resamples.
"""

import array
import asyncio
import json
import logging
import math
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFormat:
    """Raw PCM as Meta wants it: signed 16-bit little-endian, mono, 24 kHz (its native rate)."""

    sample_rate: int = 24_000
    encoding: str = "pcm_s16le"
    channels: int = 1
    sample_bytes: int = 2
    meta_encoding: str = "PCM_24KHZ"  # the handshake's audioEncoding

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.sample_bytes * self.channels

    def chunk_bytes(self, ms: int) -> int:
        return self.bytes_per_second * ms // 1000


AUDIO_FORMAT = AudioFormat()
CHUNK_MS = 100  # the browser and the CLI send 100 ms chunks

MODE = "ENDPOINTING"
HANDSHAKE_TIMEOUT_S = 10.0  # Meta closes a socket with no handshake after 10 s
FINISH_TIMEOUT_S = 5.0  # how long finish() waits for Meta to flush and close
FILE_TIMEOUT_S = 30.0
# Fallback end of turn, in silence Meta has processed after the last speech: 700 ms when Meta
# never opened the turn (its endpointing isn't working), 2 s when it did but sent no speechEnd.
# Meta itself ends a turn 540 to 700+ ms after speech, so 700 ms would race it.
FALLBACK_SILENCE_MS = 700
SAFETY_SILENCE_MS = 2000
# A chunk below this RMS (of 32767) counts as silence.
SILENCE_RMS = 500


class STTUnavailable(Exception):
    """Speech to text can't be used for this session; the UI should switch to tap buttons."""


@dataclass(frozen=True)
class Partial:
    text: str


@dataclass(frozen=True)
class Final:
    text: str


@dataclass(frozen=True)
class EndOfTurn:
    fallback: bool = False  # True when our silence rule ended the turn, not Meta


STTEvent = Partial | Final | EndOfTurn


@dataclass
class STTStats:
    connect_ms: float | None = None
    first_partial_ms: float | None = None
    speech_end_to_final_ms: list[float] = field(default_factory=list)
    audio_seconds_sent: float = 0.0


_END = object()

Connect = Callable[[str], Awaitable[object]]


async def _default_connect(url: str):
    return await ws_connect(url, open_timeout=HANDSHAKE_TIMEOUT_S)


def _rms(chunk: bytes) -> float:
    samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _settings() -> tuple[str, str, str]:
    settings = get_settings()
    api_key = settings.meta_api_key.get_secret_value()
    if not settings.model_stt or not api_key:
        raise STTUnavailable("MODEL_STT and META_API_KEY must be set in .env")
    return settings.model_stt, api_key, settings.stt_realtime_url


class StreamingSTT:
    """One streaming session. Use as `async with StreamingSTT() as stt:`.

    send_audio() takes AUDIO_FORMAT chunks; `async for event in stt` yields Partial, Final and
    EndOfTurn; finish() ends input and lets Meta flush, after which the iteration stops.
    If the socket drops it reconnects once; after that it raises STTUnavailable.
    """

    def __init__(self, connect: Connect | None = None) -> None:
        self._connect = connect or _default_connect
        self._ws = None
        self._gen = 0  # bumps on every (re)connect
        self._reconnected = False
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._receiver: asyncio.Task | None = None
        self._finishing = False
        self._closed = False
        self._failed: STTUnavailable | None = None
        self.stats = STTStats()
        self._reset_audio()

    def _reset_audio(self) -> None:
        """Per-connection state: audio time and the open turn restart with each Meta session."""
        self._audio_ms = 0.0  # audio sent on this connection
        self._last_voice_ms: float | None = None  # where the last voiced chunk ended
        self._last_voice_t: float | None = None  # wall time it was sent
        self._first_send_t: float | None = None
        self._turn_id: int | None = None
        self._meta_opened = False  # Meta sent speechStart for the current turn
        self._turn_closed = False  # we already ended Meta's current turn with a fallback
        self._partial = ""
        self._speech_ended = False
        self._fallback_turns: set[int | None] = set()

    async def __aenter__(self) -> "StreamingSTT":
        try:
            await self._open()
        except STTUnavailable as first:
            log.warning("stt connect failed (%s), retrying once", first)
            self._reconnected = True
            await self._open()
        self._receiver = asyncio.create_task(self._receive())
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close()

    def __aiter__(self) -> "StreamingSTT":
        return self

    async def __anext__(self) -> STTEvent:
        item = await self._queue.get()
        if item is _END:
            self._queue.put_nowait(_END)  # keep later calls stopped too
            raise StopAsyncIteration
        if isinstance(item, STTUnavailable):
            raise item
        return item

    async def _open(self) -> None:
        model, api_key, url = _settings()
        if not url:
            raise STTUnavailable("STT_REALTIME_URL must be set in .env")
        session_id = f"stream-{uuid.uuid4()}"
        started = time.perf_counter()
        try:
            ws = await self._connect(f"{url}?sessionId={session_id}")
            await ws.send(json.dumps({
                "authorization": {"accessToken": f"Bearer {api_key}"},
                "audioEncoding": AUDIO_FORMAT.meta_encoding,
                "model": model,
                "mode": MODE,
                "partialMode": "CUMULATIVE",
                "emitAudioProgress": True,
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT_S))
        except Exception as err:
            raise STTUnavailable(f"could not open the STT session: {type(err).__name__}: {err}") from err
        if "sessionId" not in ack:
            await ws.close()
            raise STTUnavailable(f"STT handshake rejected: {ack.get('message', ack)}")
        connect_ms = (time.perf_counter() - started) * 1000
        if self.stats.connect_ms is None:
            self.stats.connect_ms = connect_ms
        log.info("stt connected session=%s mode=%s connect_ms=%.0f", ack["sessionId"], MODE, connect_ms)
        self._ws = ws
        self._gen += 1
        self._reset_audio()

    async def _drop(self, gen: int, reason: str) -> None:
        """The socket for connection `gen` dropped: reconnect once, else raise STTUnavailable."""
        async with self._lock:
            if gen != self._gen:
                return  # the other side (sender or receiver) already reconnected
            if self._reconnected:
                raise STTUnavailable(f"STT connection dropped again: {reason}")
            self._reconnected = True
            if self._partial:
                log.warning("stt dropped an unfinished turn: %r", self._partial)
            log.warning("stt connection dropped (%s), reconnecting once", reason)
            await self._open()

    def _fail(self, err: STTUnavailable) -> None:
        log.warning("stt unavailable: %s", err)
        self._failed = err
        self._queue.put_nowait(err)

    async def send_audio(self, chunk: bytes) -> None:
        """Send one chunk of AUDIO_FORMAT audio. Raises STTUnavailable once the session is lost."""
        if self._failed:
            raise self._failed
        if self._finishing or self._ws is None:
            raise RuntimeError("send_audio after finish() or outside `async with`")
        gen = self._gen
        try:
            await self._ws.send(chunk)
        except ConnectionClosed as err:
            try:
                await self._drop(gen, f"send failed: {err}")
                await self._ws.send(chunk)
            except STTUnavailable as fail:
                self._fail(fail)
                raise
            except ConnectionClosed as again:
                fail = STTUnavailable(f"STT connection dropped again: {again}")
                self._fail(fail)
                raise fail from again
        self._track(chunk)

    def _track(self, chunk: bytes) -> None:
        now = time.perf_counter()
        if self._first_send_t is None:
            self._first_send_t = now
        ms = len(chunk) * 1000 / AUDIO_FORMAT.bytes_per_second
        self._audio_ms += ms
        self.stats.audio_seconds_sent += ms / 1000
        if _rms(chunk) >= SILENCE_RMS:
            self._last_voice_ms = self._audio_ms
            self._last_voice_t = now

    async def finish(self) -> None:
        """End input, let Meta flush the last results, then close. Safe to call twice."""
        if self._finishing:
            return
        self._finishing = True
        if self._ws is not None and not self._failed:
            try:
                await self._ws.send(json.dumps({"type": "endStream"}))
            except ConnectionClosed:
                pass
        if self._receiver is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._receiver), FINISH_TIMEOUT_S)
            except TimeoutError:
                log.warning("stt: Meta did not close %.0fs after endStream", FINISH_TIMEOUT_S)
        await self._close()

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._receiver is not None and not self._receiver.done():
            self._receiver.cancel()
            try:
                await self._receiver
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._queue.put_nowait(_END)
        log.info("stt closed audio_seconds_sent=%.1f", self.stats.audio_seconds_sent)

    async def _receive(self) -> None:
        while True:
            gen, ws = self._gen, self._ws
            try:
                while True:
                    message = await ws.recv()
                    if isinstance(message, str):
                        self._handle(json.loads(message))
            except ConnectionClosedOK as err:
                if self._finishing:
                    break
                reason = f"server closed the session ({err})"
            except ConnectionClosed as err:
                if self._finishing:
                    break
                reason = str(err)
            except Exception as err:
                reason = f"{type(err).__name__}: {err}"
            try:
                await self._drop(gen, reason)
            except STTUnavailable as err:
                self._fail(err)
                return
        # Input is over; a turn Meta never completed still gets its text.
        self._end_turn(self._partial, fallback=True)
        self._queue.put_nowait(_END)

    def _handle(self, event: dict) -> None:
        kind = event.get("type")
        log.debug("stt event %s", event)
        if kind == "speechStart":
            self._turn_id = event.get("turnId")
            self._meta_opened = True
            self._turn_closed = False
            self._partial = ""
            self._speech_ended = False
        elif kind == "transcript" and event.get("transcript"):
            if event.get("final"):  # only sent in PUSH_TO_TALK
                self._end_turn(event["transcript"], fallback=False)
            elif self._turn_closed:
                log.info("stt partial for turn %s after our fallback; ignored", self._turn_id)
            else:
                self._partial = event["transcript"]
                if self.stats.first_partial_ms is None and self._first_send_t is not None:
                    self.stats.first_partial_ms = (time.perf_counter() - self._first_send_t) * 1000
                    log.info("stt first_partial_ms=%.0f", self.stats.first_partial_ms)
                self._queue.put_nowait(Partial(event["transcript"]))
        elif kind == "speechEnd":
            self._speech_ended = True
        elif kind == "speechComplete":
            turn_id = event.get("turnId")
            if turn_id in self._fallback_turns:
                log.info("stt Meta completed turn %s after our fallback; ignored", turn_id)
            else:
                self._end_turn(event.get("transcript", ""), fallback=False)
            self._meta_opened = False
        elif kind == "error":
            log.warning("stt error from Meta: %s (session %s)", event.get("message"), event.get("sessionId"))
        self._check_silence(event.get("audioProcessedMs"))

    def _check_silence(self, processed_ms: int | None) -> None:
        """Fallback: Meta has processed enough silence past the last speech and not ended the turn."""
        silence_ms = SAFETY_SILENCE_MS if self._meta_opened else FALLBACK_SILENCE_MS
        if (
            processed_ms is None
            or not self._partial
            or self._speech_ended
            or self._last_voice_ms is None
            or processed_ms < self._last_voice_ms + silence_ms
        ):
            return
        log.info("stt fallback end of turn: %d ms of silence and no speechEnd from Meta", silence_ms)
        if self._meta_opened:
            self._fallback_turns.add(self._turn_id)
            self._turn_closed = True
        self._end_turn(self._partial, fallback=True)

    def _end_turn(self, text: str, fallback: bool) -> None:
        text = text.strip()
        self._partial = ""
        if not text:
            return
        if self._last_voice_t is not None:
            ms = (time.perf_counter() - self._last_voice_t) * 1000
            self.stats.speech_end_to_final_ms.append(ms)
            log.info(
                "stt final turn=%s speech_end_to_final_ms=%.0f source=%s",
                self._turn_id, ms, "fallback" if fallback else "meta",
            )
        self._queue.put_nowait(Final(text))
        self._queue.put_nowait(EndOfTurn(fallback=fallback))


async def transcribe_file(path: str | Path) -> str:
    """Transcribe a WAV (mono 16-bit PCM, 16 or 24 kHz) with POST /asr/transcribe. Raises on failure."""
    model, api_key, _ = _settings()
    base_url = get_settings().meta_base_url
    if not base_url:
        raise STTUnavailable("META_BASE_URL must be set in .env")
    path = Path(path)
    request = {"model": model, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=FILE_TIMEOUT_S) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/asr/transcribe",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            files={
                "request": (None, json.dumps(request), "application/json"),
                "audio": (path.name, path.read_bytes(), "audio/wav"),
            },
        )
    response.raise_for_status()
    body = response.json()
    log.info(
        "stt file session=%s audio_ms=%s latency_ms=%.0f",
        body.get("sessionId"), body.get("audioDurationMs"), (time.perf_counter() - started) * 1000,
    )
    return body.get("transcript", "").strip()
