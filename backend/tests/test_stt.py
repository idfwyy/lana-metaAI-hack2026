"""stt.py with the WebSocket mocked. Only the live test hits the network."""

import asyncio
import json
import wave
from pathlib import Path

import pytest
from pydantic import SecretStr
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from app.config import get_settings
from app.voice import stt
from app.voice.stt import AUDIO_FORMAT, CHUNK_MS, EndOfTurn, Final, Partial, StreamingSTT, STTUnavailable

HELLO_WAV = Path(__file__).parent / "fixtures" / "audio" / "hello.wav"

LOUD = (8000).to_bytes(2, "little", signed=True) * (AUDIO_FORMAT.chunk_bytes(CHUNK_MS) // 2)
SILENT = bytes(AUDIO_FORMAT.chunk_bytes(CHUNK_MS))


class FakeWS:
    """Stands in for a websockets connection: the test pushes server messages, sends are recorded."""

    def __init__(self, ack: dict | None = None) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.inbox.put_nowait(json.dumps(ack or {"sessionId": "s1"}))
        self.sent: list[str | bytes] = []
        self.closed = False
        self.send_error: Exception | None = None

    def push(self, *events: dict | Exception) -> None:
        for event in events:
            self.inbox.put_nowait(event if isinstance(event, Exception) else json.dumps(event))

    def close_ok(self) -> None:
        self.push(ConnectionClosedOK(Close(1000, ""), Close(1000, ""), rcvd_then_sent=True))

    def drop(self) -> None:
        self.push(ConnectionClosedError(Close(1011, "backend failure"), None))

    async def send(self, data) -> None:
        if self.send_error and isinstance(data, bytes):  # the handshake still goes through
            raise self.send_error
        self.sent.append(data)
        if data == json.dumps({"type": "endStream"}):
            self.close_ok()  # Meta flushes, then closes with 1000

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class FakeConnect:
    """Hands out the given sockets in order; an Exception in the list is raised instead."""

    def __init__(self, *sockets) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []

    async def __call__(self, url: str):
        self.urls.append(url)
        item = self.sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def stt_settings(request, monkeypatch):
    settings = get_settings()
    if request.node.get_closest_marker("live"):
        return settings  # the live test uses the real .env
    monkeypatch.setattr(settings, "meta_api_key", SecretStr("test-key"))
    monkeypatch.setattr(settings, "model_stt", "muse-voice-transcribe-test")
    monkeypatch.setattr(settings, "stt_realtime_url", "wss://example.test/v1/asr/realtime")
    return settings


async def _next(session: StreamingSTT, n: int) -> list:
    return [await asyncio.wait_for(anext(session), 1) for _ in range(n)]


async def test_handshake_opens_endpointing_session():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        await session.finish()
    handshake = json.loads(ws.sent[0])
    assert handshake == {
        "authorization": {"accessToken": "Bearer test-key"},
        "audioEncoding": "PCM_24KHZ",
        "model": "muse-voice-transcribe-test",
        "mode": "ENDPOINTING",
        "partialMode": "CUMULATIVE",
        "emitAudioProgress": True,
    }


async def test_meta_messages_parse_into_events():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        await session.send_audio(LOUD)
        ws.push(
            {"type": "speechStart", "turnId": 0, "audioProcessedMs": 0},
            {"type": "audioProgress", "audioProcessedMs": 80},
            {"type": "transcript", "transcript": "Hi,", "final": False, "audioProcessedMs": 100},
            {"type": "transcript", "transcript": "Hi, Lana.", "final": False, "audioProcessedMs": 200},
            {"type": "speechEnd", "turnId": 0, "audioProcessedMs": 300},
            {"type": "somethingNew", "audioProcessedMs": 300},
            {"type": "speechComplete", "turnId": 0, "transcript": "Hi, Lana.", "audioProcessedMs": 300},
        )
        events = await _next(session, 4)
    assert events == [Partial("Hi,"), Partial("Hi, Lana."), Final("Hi, Lana."), EndOfTurn(fallback=False)]
    assert ws.sent[1] == LOUD
    assert session.stats.first_partial_ms is not None
    assert len(session.stats.speech_end_to_final_ms) == 1
    assert session.stats.audio_seconds_sent == pytest.approx(0.1)


async def test_700ms_fallback_when_meta_sends_no_turn_events():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        for chunk in [LOUD] * 3 + [SILENT] * 8:  # speech ends at 300 ms
            await session.send_audio(chunk)
        ws.push(
            {"type": "transcript", "transcript": "I want braces", "final": False, "audioProcessedMs": 300},
            {"type": "audioProgress", "audioProcessedMs": 900},  # 600 ms of silence: not yet
        )
        assert await _next(session, 1) == [Partial("I want braces")]
        await asyncio.sleep(0.05)
        assert session._queue.empty()

        ws.push({"type": "audioProgress", "audioProcessedMs": 1000})  # 700 ms: fallback
        assert await _next(session, 2) == [Final("I want braces"), EndOfTurn(fallback=True)]
        await session.finish()
        assert [e async for e in session] == []


async def test_turn_meta_opened_waits_for_meta_then_2s_safety_net():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        for chunk in [LOUD] * 3 + [SILENT] * 22:
            await session.send_audio(chunk)
        ws.push(
            {"type": "speechStart", "turnId": 0, "audioProcessedMs": 0},
            {"type": "transcript", "transcript": "and I need", "final": False, "audioProcessedMs": 300},
            {"type": "audioProgress", "audioProcessedMs": 1200},  # 900 ms: Meta still deciding
        )
        assert await _next(session, 1) == [Partial("and I need")]
        await asyncio.sleep(0.05)
        assert session._queue.empty()

        ws.push({"type": "audioProgress", "audioProcessedMs": 2300})  # 2 s: safety net
        assert await _next(session, 2) == [Final("and I need"), EndOfTurn(fallback=True)]

        # Meta catching up on the same turn must not give a second Final.
        ws.push(
            {"type": "transcript", "transcript": "and I need evenings", "final": False, "audioProcessedMs": 2400},
            {"type": "audioProgress", "audioProcessedMs": 2500},
            {"type": "speechComplete", "turnId": 0, "transcript": "And I need evenings.", "audioProcessedMs": 2500},
        )
        await session.finish()
        assert [e async for e in session] == []


async def test_no_fallback_once_meta_saw_speech_end():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        for chunk in [LOUD] * 3 + [SILENT] * 10:
            await session.send_audio(chunk)
        ws.push(
            {"type": "speechStart", "turnId": 0, "audioProcessedMs": 0},
            {"type": "transcript", "transcript": "evenings", "final": False, "audioProcessedMs": 300},
            {"type": "speechEnd", "turnId": 0, "audioProcessedMs": 300},
            {"type": "audioProgress", "audioProcessedMs": 1200},
            {"type": "speechComplete", "turnId": 0, "transcript": "Evenings.", "audioProcessedMs": 300},
        )
        events = await _next(session, 3)
    assert events == [Partial("evenings"), Final("Evenings."), EndOfTurn(fallback=False)]


async def test_reconnects_once_after_drop():
    first, second = FakeWS(), FakeWS()
    connect = FakeConnect(first, second)
    async with StreamingSTT(connect=connect) as session:
        first.drop()
        await asyncio.sleep(0.05)
        await session.send_audio(LOUD)
        second.push({"type": "transcript", "transcript": "still here", "final": False, "audioProcessedMs": 100})
        assert await _next(session, 1) == [Partial("still here")]
        await session.finish()
    assert len(connect.urls) == 2
    assert second.sent[1] == LOUD


async def test_unavailable_after_failed_reconnect():
    ws = FakeWS()
    connect = FakeConnect(ws, OSError("network is down"))
    async with StreamingSTT(connect=connect) as session:
        ws.drop()
        with pytest.raises(STTUnavailable):
            await asyncio.wait_for(anext(session), 1)
        with pytest.raises(STTUnavailable):
            await session.send_audio(LOUD)
    assert len(connect.urls) == 2


async def test_unavailable_when_send_fails_after_reconnect():
    first, second = FakeWS(), FakeWS()
    first.send_error = second.send_error = ConnectionClosedError(Close(1011, "gone"), None)
    connect = FakeConnect(first, second)
    async with StreamingSTT(connect=connect) as session:
        with pytest.raises(STTUnavailable):
            await session.send_audio(LOUD)
        with pytest.raises(STTUnavailable):
            await asyncio.wait_for(anext(session), 1)
    assert len(connect.urls) == 2


async def test_unavailable_when_connect_fails_twice():
    connect = FakeConnect(OSError("refused"), OSError("refused"))
    with pytest.raises(STTUnavailable):
        async with StreamingSTT(connect=connect):
            pass
    assert len(connect.urls) == 2


async def test_rejected_handshake_is_unavailable():
    rejected = {"type": "error", "message": "bad key"}
    connect = FakeConnect(FakeWS(ack=rejected), FakeWS(ack=rejected))
    with pytest.raises(STTUnavailable, match="bad key"):
        async with StreamingSTT(connect=connect):
            pass


async def test_finish_closes_cleanly():
    ws = FakeWS()
    async with StreamingSTT(connect=FakeConnect(ws)) as session:
        await session.send_audio(LOUD)
        ws.push({"type": "transcript", "transcript": "half a", "final": False, "audioProcessedMs": 100})
        assert await _next(session, 1) == [Partial("half a")]
        await session.finish()
        await session.finish()  # a second call is a no-op
        rest = [e async for e in session]
        with pytest.raises(RuntimeError):
            await session.send_audio(LOUD)
    assert ws.sent[-1] == json.dumps({"type": "endStream"})
    assert ws.closed
    assert session._receiver.done()
    # Meta closed without completing the turn, so its partial text still arrives as the final.
    assert rest == [Final("half a"), EndOfTurn(fallback=True)]


def test_missing_settings_are_unavailable(stt_settings, monkeypatch):
    monkeypatch.setattr(stt_settings, "meta_api_key", SecretStr(""))
    with pytest.raises(STTUnavailable):
        asyncio.run(stt.transcribe_file(HELLO_WAV))


def test_fixture_is_in_the_stt_format():
    with wave.open(str(HELLO_WAV), "rb") as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (
            AUDIO_FORMAT.sample_rate, AUDIO_FORMAT.sample_bytes, AUDIO_FORMAT.channels,
        )
    assert HELLO_WAV.stat().st_size < 1_000_000


@pytest.mark.live
async def test_live_stream_hello_wav():
    with wave.open(str(HELLO_WAV), "rb") as w:
        pcm = w.readframes(w.getnframes())
    step = AUDIO_FORMAT.chunk_bytes(CHUNK_MS)
    finals = []
    async with StreamingSTT() as session:

        async def send():
            for start in range(0, len(pcm), step):
                await session.send_audio(pcm[start:start + step])
                await asyncio.sleep(CHUNK_MS / 1000)
            await session.finish()

        sender = asyncio.create_task(send())
        async for event in session:
            if isinstance(event, Final):
                finals.append(event.text)
        await sender
    assert "braces" in " ".join(finals).lower()
