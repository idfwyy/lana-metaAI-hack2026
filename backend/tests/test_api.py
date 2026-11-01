"""main.py, audio_bus.py and guard.py through FastAPI's TestClient. Orchestrator, TTS, STT and the LLM mocked; no network."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import guard, main
from app.agent import commands as commands_module
from app.agent import intake as intake_module
from app.agent.intake import Intake
from app.agent.ranker import rank_offers
from app.cli import DEMO_PREFERENCES
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.db.store import store
from app.integrations import meta_llm
from app.schemas.enums import CallStatus, CaseStatus, Speaker
from app.schemas.events import Booked, CallbackStarted, CallStatusEvent, CaseStatusEvent, LogLine, TranscriptFinal
from app.schemas.quote import Quote
from app.voice import audio_bus, tts
from app.voice.stt import AUDIO_FORMAT, CHUNK_MS, Final, Partial, STTUnavailable

TAGGED_LINE = "[warmly] Hi there! This is Lana. [laughs]"
SHOWN_LINE = "Hi there! This is Lana."
CLINIC_LINE = "Thanks for calling Parkway, how can I help?"

QUOTES = Path(__file__).parent / "fixtures" / "transcripts" / "three_quotes.json"
SUMMARY = "Parkway Orthodontics came in at $5,050 all in."

LOUD = (8000).to_bytes(2, "little", signed=True) * (AUDIO_FORMAT.chunk_bytes(CHUNK_MS) // 2)


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
        # STT: what the fake stream yields after its first voiced chunk, and every chunk sent.
        self.stt_script: list = []
        self.stt_error: Exception | None = None
        self.stt_sent: list[bytes] = []
        self.heard: list[dict] = []  # intake's call_json replies
        self.classified: list[str] = []  # texts the command classifier saw
        self.command: str | None = None  # what the classifier answers
        self.stt_opened = 0  # STT sessions opened
        self.charged: list[float] = []  # STT seconds charged to the case
        self.preferences: list = []  # each run's preferences
        self.outcome: list[str] = []  # "booked" / "saved"


class FakeSTT:
    def __init__(self, world: "World") -> None:
        self.world = world
        self.events: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "FakeSTT":
        if self.world.stt_error is not None:
            raise self.world.stt_error
        self.world.stt_opened += 1
        return self

    async def __aexit__(self, *exc) -> None:
        self.events.put_nowait(None)

    async def finish(self) -> None:
        self.events.put_nowait(None)

    def __aiter__(self) -> "FakeSTT":
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send_audio(self, chunk: bytes) -> None:
        self.world.stt_sent.append(chunk)
        if any(chunk):
            while self.world.stt_script:
                self.events.put_nowait(self.world.stt_script.pop(0))


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()

    class FakeOrchestrator:
        def __init__(self, case, emit, max_clinics=3) -> None:
            self.case, self.emit = case, emit
            self.summary = None
            w.max_clinics.append(max_clinics)
            w.preferences.append(case.preferences)

        async def start_callback(self):
            quotes = [Quote.model_validate(q) for q in json.loads(QUOTES.read_text(encoding="utf-8"))]
            prefs = DEMO_PREFERENCES.model_copy(update={"budget_max": 10_000})
            self.case.offers = rank_offers(quotes, get_public_clinics(), prefs)
            self.case.status = CaseStatus.CALLBACK
            self.summary = SUMMARY
            await self.emit(CallbackStarted(case_id=self.case.id, summary=SUMMARY))
            return SUMMARY

        async def book(self):
            w.outcome.append("booked")
            top = self.case.offers[0]
            slot = min(top.quote.checklist.next_slots.value)
            event = Booked(case_id=self.case.id, clinic_id=top.clinic.id, slot=slot, total_saved=0)
            self.case.status = CaseStatus.BOOKED
            await self.emit(event)
            return event

        async def save(self):
            w.outcome.append("saved")
            self.case.status = CaseStatus.SAVED
            await self.emit(
                CaseStatusEvent(case_id=self.case.id, status=CaseStatus.SAVED, previous=CaseStatus.CALLBACK)
            )

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

    async def fake_intake_json(prompt, schema, model, images=None, reasoning=None):
        return schema.model_validate(w.heard.pop(0) if w.heard else {})

    async def fake_classifier(prompt, schema, model, images=None, reasoning=None):
        w.classified.append(prompt)
        return schema.model_validate({"command": w.command})

    real_charge = guard.charge_stt

    def charge_stt(seconds: float) -> None:
        w.charged.append(seconds)
        real_charge(seconds)

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
    monkeypatch.setattr(main, "_prewarm_lines", no_warm_up)
    monkeypatch.setattr(main, "StreamingSTT", lambda: FakeSTT(w))
    monkeypatch.setattr(main, "LINE_TAIL_S", 0)
    monkeypatch.setattr(intake_module, "call_json", fake_intake_json)
    monkeypatch.setattr(commands_module, "call_json", fake_classifier)
    monkeypatch.setattr(audio_bus, "stream_speech", fake_speech)
    monkeypatch.setattr(guard, "charge_stt", charge_stt)
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


def test_clinic_mode_is_not_built_yet(client) -> None:
    response = client.post("/cases", json={"mode": "clinic"})
    assert response.status_code == 501
    assert "not built yet" in response.json()["detail"]


def test_patient_case_starts_without_preferences(client) -> None:
    case_id = _new_case(client, mode="patient")
    assert store.get(case_id).case.preferences is None


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
            run += _until(ws, lambda m: m["type"] == "callback_started")  # Lana rings back
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


def test_stt_counter_stops_at_the_limit(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_stt_seconds_per_case", 1)
    guard.charge_stt(10**6)  # no case: never limited

    usage = guard.CaseUsage("c1")
    with guard.case_usage(usage):
        for _ in range(10):
            guard.charge_stt(0.1)
        with pytest.raises(guard.LimitHit):
            guard.charge_stt(0.1)
    assert usage.hit == "stt" and usage.stt_seconds == pytest.approx(1.0)


# ---------- the patient's voice session ----------


def _until(ws, done) -> list[dict]:
    """Messages up to and including the first one `done` accepts."""
    messages = []
    while True:
        msg = ws.receive_json()
        messages.append(msg)
        if done(msg):
            return messages


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _listen(client: TestClient):
    return client.websocket_connect(f"/ws/case/{_new_case(client, mode='patient')}")


def _greeted(ws) -> list[dict]:
    """Send listen and read Lana's greeting through its last audio frame."""
    ws.send_json({"type": "listen"})
    messages = _until(ws, lambda m: m["type"] == "audio" and m["last"])
    time.sleep(0.05)  # past her line's end on the server clock (Windows ticks about every 15 ms)
    return messages


def _question(topic: str) -> str:
    return Intake()._lines[topic]


def test_listen_greets_with_the_demo_heads_up(client, world) -> None:
    with _listen(client) as ws:
        messages = _greeted(ws)
    greeting = messages[0]
    assert greeting["type"] == "agent_said" and greeting["call_id"] is None
    assert greeting["text"] == main._greeting()
    assert "live demo" in greeting["text"] and "—" not in greeting["text"]
    assert world.spoken[0][1] == "lana-voice"


def test_binary_frames_reach_the_stt(client, world) -> None:
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        _wait_for(lambda: LOUD in world.stt_sent)


def test_audio_during_lanas_line_is_not_sent_or_counted(client, world, monkeypatch) -> None:
    monkeypatch.setattr(main, "LINE_TAIL_S", 30)  # her greeting is still playing
    with _listen(client) as ws:
        _greeted(ws)
        for _ in range(3):
            ws.send_bytes(LOUD)
        time.sleep(0.3)
    assert world.stt_sent == []
    assert world.stt_opened == 0
    assert world.charged == []


def test_stt_session_closes_while_lana_talks_and_reopens(client, world) -> None:
    world.stt_script = [Final("I want braces")]
    world.heard = [{"treatment": "braces"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        _until(ws, lambda m: m["type"] == "audio" and m["last"])  # Lana's next question
        _wait_for(lambda: world.stt_opened == 1 and not world.stt_script)
        time.sleep(0.2)
        ws.send_bytes(LOUD)
        _wait_for(lambda: world.stt_opened == 2)
    # Only real mic frames were charged, never the silence that kept the stream paced.
    assert world.charged and all(s == CHUNK_MS / 1000 for s in world.charged)
    assert len(world.charged) == sum(1 for chunk in world.stt_sent if any(chunk))


def test_a_final_transcript_runs_intake_and_lana_asks_the_next_question(client, world) -> None:
    world.stt_script = [Partial("I want braces"), Final("I want braces near Dallas")]
    world.heard = [{"treatment": "braces", "city": "Dallas"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "agent_said")

    kinds = [m["type"] for m in messages]
    assert kinds == ["user_partial", "user_said", "intake", "agent_said"]
    assert messages[0]["text"] == "I want braces"
    assert messages[1]["text"] == "I want braces near Dallas"
    update = messages[2]
    assert update["topic"] == "budget" and update["done"] is False
    assert update["draft"] == {"treatment": "braces", "zip_code": "75201", "city": "Dallas"}
    assert messages[3]["text"] == _question("budget")
    assert [m["seq"] for m in messages] == sorted(m["seq"] for m in messages)


def test_typed_text_takes_the_same_path(client, world) -> None:
    world.heard = [{"treatment": "aligners"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Clear aligners"})
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    assert [m["type"] for m in messages] == ["user_said", "intake", "agent_said"]
    assert messages[2]["text"] == _question("location")


def test_intake_done_sends_the_preferences(client, world) -> None:
    world.heard = [{
        "treatment": "braces", "city": "Dallas", "budget": "5000", "prefers_monthly": True,
        "has_xrays": False, "has_plan": False, "must_haves": ["evening hours"],
    }]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Braces near Dallas under 5k, monthly, evenings, no x-rays"})
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    update = messages[1]
    assert update["done"] is True and update["topic"] is None
    assert update["preferences"]["budget_max"] == 5000
    assert update["preferences"]["must_haves"] == ["evening hours"]
    assert messages[2]["text"].startswith("Got it. Braces near Dallas, under $5,000")
    assert messages[2]["text"].endswith(
        "I'm calling clinics now. You can watch, or step away and I'll call you back."
    )
    assert store.get(update["case_id"]).case.preferences.zip_code == "75201"


def test_saying_just_show_me_starts_the_watch_run(client, world) -> None:
    world.stt_script = [Final("Just show me.")]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "case_status" and m["status"] == "offers_ready")
    assert messages[0] == {**messages[0], "type": "user_said", "text": "Just show me."}
    assert world.runs == 1
    assert world.classified == []  # a keyword matched; no LLM


def test_clinic_mode_is_coming_soon_and_start_over_resets(client, world) -> None:
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "I'll be the clinic"})
        said = _until(ws, lambda m: m["type"] == "agent_said")
        ws.send_json({"type": "user_text", "text": "Start over"})
        reset = _until(ws, lambda m: m["type"] == "command")
    assert said[-1]["text"] == main.COMING_SOON
    assert reset[-1]["name"] == "start_over"
    assert world.runs == 0


def test_stt_failure_sends_stt_unavailable(client, world) -> None:
    world.stt_error = STTUnavailable("Meta is down")
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        msg = ws.receive_json()
        assert msg["type"] == "stt_unavailable"
        # Typing still works.
        world.heard = [{"treatment": "braces"}]
        ws.send_json({"type": "user_text", "text": "Braces"})
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    assert messages[-1]["text"] == _question("location")


def test_stt_seconds_limit_counts_only_mic_audio(client, world, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_stt_seconds_per_case", 1)
    with _listen(client) as ws:
        _greeted(ws)
        for _ in range(11):
            ws.send_bytes(LOUD)
            time.sleep(0.03)
        msg = ws.receive_json()
        assert msg == {**msg, "type": "error", "code": "limit", "message": guard.LIMIT_MESSAGE}
        sent = len(world.stt_sent)
        assert sum(1 for chunk in world.stt_sent if any(chunk)) == 10  # 1 s of 100 ms mic chunks
        ws.send_bytes(LOUD)
        time.sleep(0.3)
    assert len(world.stt_sent) == sent


def test_stt_limit_default_is_300_seconds() -> None:
    assert type(get_settings()).model_fields["max_stt_seconds_per_case"].default == 300


# ---------- patient mode end to end ----------

FULL_INTAKE = {
    "treatment": "braces", "city": "Dallas", "budget": "5000", "prefers_monthly": True,
    "has_xrays": False, "has_plan": False, "must_haves": ["evening hours"],
}
FULL_REPLY = "Braces near Dallas under 5k, monthly, evenings, no x-rays"


def _ringing(ws) -> list[dict]:
    """Through the callback's summary event."""
    return _until(ws, lambda m: m["type"] == "callback_started")


def test_intake_done_starts_the_calls_with_the_patients_preferences(client, world) -> None:
    world.heard = [FULL_INTAKE]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": FULL_REPLY})
        messages = _ringing(ws)
    kinds = [m["type"] for m in messages]
    assert kinds.index("agent_said") < kinds.index("case_status")  # her line, then the calls
    assert world.runs == 1 and world.max_clinics == [2]
    prefs = world.preferences[-1]
    assert prefs.zip_code == "75201" and prefs.budget_max == 5000 and prefs.must_haves == ["evening hours"]
    assert messages[-1]["summary"] == SUMMARY


def test_saying_ill_wait_during_the_calls_steps_away(client, world) -> None:
    world.hang = True  # the calls keep running
    world.heard = [FULL_INTAKE]
    world.stt_script = [Final("The total is $4,800."), Final("I'll wait.")]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": FULL_REPLY})
        _until(ws, lambda m: m["type"] == "case_status")
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "command")
        case_id = messages[-1]["case_id"]
        _wait_for(lambda: store.get(case_id).voice is False)
    assert messages[-1]["name"] == "step_away"
    assert not any(m["type"] == "user_said" for m in messages)  # clinic speech is ignored
    assert world.classified == []


def test_tapping_step_away_turns_call_audio_off_and_back_on(client, world) -> None:
    world.hang = True
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        _until(ws, lambda m: m["type"] == "case_status")
        ws.send_json({"type": "command", "name": "step_away"})
        _wait_for(lambda: store.get(case_id).voice is False)
        ws.send_json({"type": "command", "name": "step_back"})
        _wait_for(lambda: store.get(case_id).voice is True)


def _answered(ws) -> list[dict]:
    """Answer the ringing call and read Lana's summary line through its audio."""
    _ringing(ws)
    ws.send_json({"type": "answer"})
    return _until(ws, lambda m: m["type"] == "audio" and m["last"])


@pytest.mark.parametrize("reply, command", [("Yes, book it please", "yes_book"), ("Book it", None)])
def test_watch_callback_yes_books_the_first_slot(client, world, reply, command) -> None:
    world.command = command
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        said = _answered(ws)
        ws.send_json({"type": "user_text", "text": reply})
        messages = _until(ws, lambda m: m["type"] == "booked")
    assert said[0]["type"] == "agent_said" and said[0]["text"] == f"{SUMMARY} {main.BOOK_QUESTION}"
    line = next(m for m in messages if m["type"] == "agent_said")
    assert line["text"].startswith("You're booked at ")
    assert line["text"].endswith("This is a demo clinic, so no real appointment was made.")
    assert messages.index(line) < len(messages) - 1  # her line, then the booked event
    assert world.outcome == ["booked"]


def test_patient_callback_not_yet_saves(client, world) -> None:
    world.heard = [FULL_INTAKE]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": FULL_REPLY})
        _answered(ws)
        ws.send_json({"type": "user_text", "text": "Not yet"})
        messages = _until(ws, lambda m: m["type"] == "case_status" and m["status"] == "saved")
    line = next(m for m in messages if m["type"] == "agent_said")
    assert line["text"] == main.SAVED_LINE
    assert world.outcome == ["saved"]


def test_two_unclear_callback_answers_save_without_booking(client, world) -> None:
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        _answered(ws)
        ws.send_json({"type": "user_text", "text": "What was the hold music?"})
        again = _until(ws, lambda m: m["type"] == "agent_said")
        ws.send_json({"type": "user_text", "text": "Hmm, purple"})
        _until(ws, lambda m: m["type"] == "case_status" and m["status"] == "saved")
    assert again[-1]["text"] == main.BOOK_AGAIN
    assert world.outcome == ["saved"]


def test_step_away_phrases() -> None:
    assert main.is_step_away("I'll wait.")
    assert main.is_step_away("Okay Lana, call me back")
    assert not main.is_step_away("The total is $4,800.")
    assert not main.is_step_away("I'll wait for the manager to check")
