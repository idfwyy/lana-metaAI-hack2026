"""Sends a case's events and both sides' audio to the browser over one socket, in one seq order.

For each Lana or clinic line: the text event first (audio tags stripped), then that line's
mp3 as audio frames that point back to it with line_seq. Lines are voiced one at a time;
emit() returns only after the line's last frame is sent. If voice is off or TTS fails, a
single failed marker follows the text so the browser never waits for audio that won't come.
"""

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from app import guard
from app.data.loader import get_public_clinics
from app.db.store import CaseRecord
from app.schemas.clinic import Clinic
from app.schemas.enums import Speaker
from app.schemas.events import AgentSaid, CallStatusEvent, TranscriptFinal
from app.voice.tts import VoiceConfigError, stream_speech, strip_audio_tags, voice_for

log = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]


class AudioBus:
    def __init__(self, record: CaseRecord, send: Send) -> None:
        self.record = record
        self._send_raw = send
        self._lock = asyncio.Lock()
        self._clinics: dict[str, Clinic] = {c.id: c for c in get_public_clinics()}
        self._call_clinic: dict[str, str] = {}

    async def _send(self, payload: dict) -> int:
        seq = self.record.next_seq()
        await self._send_raw({**payload, "seq": seq, "case_id": self.record.case.id})
        return seq

    async def send_payload(self, payload: dict) -> int:
        """Send one message with the next seq. Waits for a line in progress to finish."""
        async with self._lock:
            return await self._send(payload)

    async def emit(self, event: BaseModel) -> None:
        """The orchestrator's emit. After a usage limit, everything is dropped."""
        if guard.limit_hit():
            return
        if isinstance(event, CallStatusEvent):
            self._call_clinic[event.call_id] = event.clinic_id
        async with self._lock:
            if isinstance(event, AgentSaid) or (
                isinstance(event, TranscriptFinal) and event.speaker in (Speaker.AGENT, Speaker.CLINIC)
            ):
                await self._line(event)
            else:
                await self._send(event.model_dump(mode="json"))

    def _voice(self, event: AgentSaid | TranscriptFinal) -> tuple[Speaker, str | None]:
        speaker = Speaker.AGENT if isinstance(event, AgentSaid) else event.speaker
        if not self.record.voice:
            return speaker, None
        try:
            if speaker == Speaker.AGENT:
                return speaker, voice_for(speaker)
            clinic = self._clinics.get(self._call_clinic.get(event.call_id or "", ""))
            return speaker, voice_for(speaker, clinic)
        except VoiceConfigError as err:
            log.warning("case=%s no voice for a %s line: %s", self.record.case.id, speaker, err)
            return speaker, None

    async def _line(self, event: AgentSaid | TranscriptFinal) -> None:
        shown = event.model_copy(update={"text": strip_audio_tags(event.text)})
        line_seq = await self._send(shown.model_dump(mode="json"))
        speaker, voice_id = self._voice(event)
        audio = {"type": "audio", "line_seq": line_seq, "speaker": speaker.value}

        # Each chunk waits for the next, so the final one can carry last: true.
        held: bytes | None = None
        if voice_id is not None:
            try:
                async for chunk in stream_speech(event.text, voice_id, speaker=speaker):
                    if held is not None:
                        await self._send({**audio, "chunk_b64": base64.b64encode(held).decode("ascii"), "last": False})
                    held = chunk
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not guard.limit_hit():
                    log.warning("case=%s tts failed mid-line: %s: %s", self.record.case.id, type(err).__name__, err)
        if guard.limit_hit():
            return
        if held is not None:
            await self._send({**audio, "chunk_b64": base64.b64encode(held).decode("ascii"), "last": True})
        else:
            await self._send({**audio, "last": True, "failed": True})
