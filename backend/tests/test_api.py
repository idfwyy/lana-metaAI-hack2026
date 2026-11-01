"""main.py, audio_bus.py and guard.py through FastAPI's TestClient. Orchestrator and TTS mocked; no network."""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from app import guard, main
from app.config import get_settings
from app.db.store import store
from app.integrations import meta_llm
from app.schemas.enums import CallStatus, CaseStatus, Speaker
from app.schemas.events import CallStatusEvent, CaseStatusEvent, LogLine, TranscriptFinal
from app.voice import audio_bus, tts

TAGGED_LINE = "[warmly] Hi there! This is Lana. [laughs]"
SHOWN_LINE = "Hi there! This is Lana."
CLINIC_LINE = "Thanks for calling Parkway, how can I help?"


def _script(case_id: str) -> list:
    call_id = "parkway-abc123"
    return [
        CaseStatusEvent(case_id=case_id, status=CaseStatus.INTAKE, previous=CaseStatus.NEW),
        LogLine(case_id=case_id, text="Found 2 orthodontists"),
        CallStatusEvent(case_id=case_id, call_id=call_id, clinic_id="parkway", status=CallStatus.LIVE),
        TranscriptFinal(case_id=case_id, call_id=call_id, speaker=Speaker.AGENT, text=TAGGED_LINE, t_seconds=0),
        TranscriptFinal(case_id=case_id, call_id=call_id, speaker=Speaker.CLINIC, text=CLINIC_LINE, t_seconds=3),
        CaseStatusEvent(case_id=case_id, status=CaseStatus.OFFERS_READY, previous=CaseStatus.NEGOTIATING),
    ]


class World:
    def __init__(self) -> None:
        self.script = _script
        self.hang = False  # the fake orchestrator waits forever after its script
        self.cancelled = threading.Event()
        self.runs = 0
        self.max_clinics: list[int] = []
        self.spoken: list[tuple[str, str, Speaker]] = []
        self.chunks: list[bytes] = [b"aaa", b"bbb", b"ccc"]
        self.tts_error: Exception | None = None


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()

    class FakeOrchestrator:
        def __init__(self, case, emit, max_clinics=3) -> None:
            self.case, self.emit = case, emit
            w.max_clinics.append(max_clinics)

        async def run(self):
            w.runs += 1
            try:
                for event in w.script(self.case.id):
                    await self.emit(event)
                    await asyncio.sleep(0)
                if w.hang:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                w.cancelled.set()
                raise
            self.case.status = CaseStatus.OFFERS_READY
            return self.case

    async def fake_speech(text, voice_id, *, speaker, model=None, stability=None):
        w.spoken.append((text, voice_id, speaker))
        if w.tts_error:
            raise w.tts_error
        for chunk in w.chunks:
            yield chunk

    async def no_warm_up() -> None:
        return None

    settings = get_settings()
    for name, value in {
        "voice_agent": "lana-voice",
        "voice_clinic_1": "clinic-1-voice",
        "voice_clinic_2": "clinic-2-voice",
        "voice_clinic_3": "clinic-3-voice",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(main, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(main, "_warm_up_voices", no_warm_up)
    monkeypatch.setattr(main, "_open_clients", lambda: None)
    monkeypatch.setattr(audio_bus, "stream_speech", fake_speech)
    store.clear()
    guard.reset()
    yield w
    store.clear()
    guard.reset()


@pytest.fixture
def client(world) -> TestClient:
    with TestClient(main.app) as c:
        yield c


def _new_case(client: TestClient, **body) -> str:
    response = client.post("/cases", json={"mode": "watch", **body})
    assert response.status_code == 200, response.text
    return response.json()["case_id"]


def _until_done(ws) -> list[dict]:
    """Messages until the run's final case status or an error."""
    messages = []
    while True:
        msg = ws.receive_json()
        messages.append(msg)
        if msg["type"] == "error" or (msg["type"] == "case_status" and msg["status"] == "offers_ready"):
            return messages


def _run(client: TestClient, **body) -> list[dict]:
    case_id = _new_case(client, **body)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        return _until_done(ws)


def _lines(messages) -> list[dict]:
    return [m for m in messages if m["type"] == "transcript_final"]


def _audio(messages, line_seq: int) -> list[dict]:
    return [m for m in messages if m["type"] == "audio" and m["line_seq"] == line_seq]


# ---------- REST ----------


def test_health(client) -> None:
    assert client.get("/health").json() == {"ok": True}


def test_post_cases_returns_an_id(client) -> None:
    case_id = _new_case(client, voice=False, clinics=3)
    assert isinstance(case_id, str) and case_id
    record = store.get(case_id)
    assert record is not None and record.voice is False and record.clinics == 3


@pytest.mark.parametrize("mode", ["patient", "clinic"])
def test_other_modes_are_not_built_yet(client, mode) -> None:
    response = client.post("/cases", json={"mode": mode})
    assert response.status_code == 501
    assert "not built yet" in response.json()["detail"]


def test_new_cases_per_ip_are_limited(client) -> None:
    for _ in range(get_settings().max_cases_per_ip_hour):
        _new_case(client)
    response = client.post("/cases", json={"mode": "watch"})
    assert response.status_code == 429
    assert response.json() == {"code": "limit", "message": guard.LIMIT_MESSAGE}


# ---------- the stream ----------


def test_events_stream_with_increasing_seq(client, world) -> None:
    messages = _run(client, clinics=3)

    seqs = [m["seq"] for m in messages]
    assert seqs == list(range(1, len(messages) + 1))
    assert {m["type"] for m in messages} >= {"case_status", "log_line", "call_status", "transcript_final", "audio"}
    assert all(m["case_id"] == messages[0]["case_id"] for m in messages)
    assert world.max_clinics == [3]


def test_each_line_text_arrives_before_its_audio(client, world) -> None:
    messages = _run(client)

    lines = _lines(messages)
    assert len(lines) == 2
    for line in lines:
        frames = _audio(messages, line["seq"])
        assert len(frames) == len(world.chunks)
        assert all(f["seq"] > line["seq"] for f in frames)
        assert [f["last"] for f in frames] == [False, False, True]
        # A line's audio comes right after its text, before anything else.
        start = messages.index(line)
        assert messages[start + 1 : start + 1 + len(frames)] == frames
    assert [f["speaker"] for f in _audio(messages, lines[1]["seq"])] == ["clinic"] * 3
    assert [s[1] for s in world.spoken] == ["lana-voice", "clinic-1-voice"]


def test_voice_off_sends_failed_markers(client, world) -> None:
    messages = _run(client, voice=False)

    for line in _lines(messages):
        frames = _audio(messages, line["seq"])
        assert len(frames) == 1
        assert frames[0]["last"] is True and frames[0]["failed"] is True
        assert "chunk_b64" not in frames[0]
    assert world.spoken == []


@pytest.mark.parametrize("failure", ["no_audio", "raises"])
def test_tts_failure_sends_text_and_failed_marker(client, world, failure) -> None:
    if failure == "no_audio":
        world.chunks = []
    else:
        world.tts_error = RuntimeError("ElevenLabs down")

    messages = _run(client)

    lines = _lines(messages)
    assert [l["text"] for l in lines] == [SHOWN_LINE, CLINIC_LINE]
    for line in lines:
        assert _audio(messages, line["seq"]) == [
            {"type": "audio", "line_seq": line["seq"], "speaker": line["speaker"], "last": True,
             "failed": True, "seq": line["seq"] + 1, "case_id": line["case_id"]}
        ]
    assert messages[-1]["status"] == "offers_ready"


def test_shown_text_never_contains_audio_tags(client, world) -> None:
    messages = _run(client)

    for line in _lines(messages):
        assert "[" not in line["text"] and "]" not in line["text"]
    assert _lines(messages)[0]["text"] == SHOWN_LINE
    assert world.spoken[0][0] == TAGGED_LINE  # TTS still gets the tags


def test_disconnect_cancels_the_run(client, world) -> None:
    world.hang = True
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        assert ws.receive_json()["type"] == "case_status"  # the run has started
    assert world.cancelled.wait(timeout=5)


def test_stop_cancels_the_run(client, world) -> None:
    world.hang = True
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_json({"type": "stop"})
        assert world.cancelled.wait(timeout=5)


def test_unused_messages_are_ignored_and_bad_ones_rejected(client, world) -> None:
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "user_text", "text": "hello"})
        ws.send_json({"type": "command", "name": "skip"})
        ws.send_json({"type": "dance"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "bad_message" and msg["seq"] == 1
    assert world.runs == 0


def test_unknown_case_gets_not_found(client) -> None:
    with client.websocket_connect("/ws/case/nope") as ws:
        assert ws.receive_json()["code"] == "not_found"


# ---------- guard ----------


def test_guard_blocks_the_third_run(client, world) -> None:
    case_id = _new_case(client, voice=False)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        seqs = []
        for _ in range(2):
            ws.send_json({"type": "start"})
            run = _until_done(ws)
            assert run[-1]["type"] == "case_status"
            seqs += [m["seq"] for m in run]
        ws.send_json({"type": "start"})
        msg = ws.receive_json()
    assert msg["type"] == "error" and msg["code"] == "limit" and msg["message"] == guard.LIMIT_MESSAGE
    assert world.runs == 2
    assert seqs == sorted(seqs) and msg["seq"] > seqs[-1]  # one seq per case, across runs


def test_guard_stops_a_case_over_its_tts_character_limit(client, world, monkeypatch, tmp_path) -> None:
    """The real stream_speech with the ElevenLabs client faked, so the counter in tts.py runs."""
    sent: list[str] = []

    class FakeTTS:
        async def stream(self, voice_id, **kwargs):
            sent.append(kwargs["text"])
            yield b"mp3"

    class FakeClient:
        text_to_speech = FakeTTS()

    monkeypatch.setattr(audio_bus, "stream_speech", tts.stream_speech)
    monkeypatch.setattr(tts, "_client", lambda: FakeClient())
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(get_settings(), "max_tts_chars_per_case", 60)
    world.hang = True  # would run forever if the limit didn't stop it

    messages = _run(client)

    assert messages[-1] == {
        "type": "error", "code": "limit", "message": guard.LIMIT_MESSAGE,
        "seq": messages[-1]["seq"], "case_id": messages[-1]["case_id"],
    }
    assert len(sent) == 1  # Lana's line fit; the clinic line was refused before any request
    clinic = [m for m in messages if m["type"] == "transcript_final" and m["speaker"] == "clinic"]
    assert clinic and not _audio(messages, clinic[0]["seq"])
    assert not any(m["type"] == "case_status" and m["status"] == "offers_ready" for m in messages)
    assert world.cancelled.wait(timeout=5)


def test_tts_counter_does_nothing_without_a_case() -> None:
    guard.charge_tts(10**9)


async def test_llm_counter_stops_at_the_limit(monkeypatch) -> None:
    class Message:
        content = "hi"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = None

    class Completions:
        async def create(self, **kwargs):
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    monkeypatch.setattr(meta_llm, "get_client", lambda: Client())
    monkeypatch.setattr(get_settings(), "max_llm_calls_per_case", 3)

    for _ in range(5):  # no case: never limited
        await meta_llm._complete("p", "m", None, None)

    cancelled = []
    usage = guard.CaseUsage("c1", on_limit=lambda: cancelled.append(True))
    with guard.case_usage(usage):
        for _ in range(3):
            await meta_llm._complete("p", "m", None, None)
        with pytest.raises(guard.LimitHit):
            await meta_llm._complete("p", "m", None, None)
    assert usage.llm_calls == 3 and usage.hit == "llm" and cancelled == [True]
