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
from app.agent.intake import RETRY_PROMPT, TOPIC_FIELDS, Intake
from app.agent.ranker import rank_offers
from app.agent.researcher import pick_clinics
from app.cli import DEMO_PREFERENCES
from app.config import get_settings
from app.data.loader import get_public_clinics, load_demo_clinics
from app.db.store import store
from app.integrations import meta_llm
from app.schemas.enums import CallStatus, CaseStatus, DemoMode, Speaker
from app.schemas.events import Booked, CallbackStarted, CallStatusEvent, CaseStatusEvent, LogLine, TranscriptFinal
from app.schemas.quote import Quote
from app.voice import audio_bus, tts
from app.voice.stt import AUDIO_FORMAT, CHUNK_MS, Final, Partial, STTUnavailable

TAGGED_LINE = "[warmly] Hi there! This is Lana. [laughs]"
SHOWN_LINE = "Hi there! This is Lana."
CLINIC_LINE = "Thanks for calling Parkway, how can I help?"

QUOTES = Path(__file__).parent / "fixtures" / "transcripts" / "three_quotes.json"
SUMMARY = "Parkway Orthodontics came in at $5,050 all in."
PRICE_QUESTION = "How much is the full treatment at your office?"

LOUD = (8000).to_bytes(2, "little", signed=True) * (AUDIO_FORMAT.chunk_bytes(CHUNK_MS) // 2)

REAL_DEMO_INTAKE = main.demo_intake  # the world fixture swaps it out for most tests


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
        self.intake_delay = 0.0  # seconds intake's call_json takes
        self.chunk_delay = 0.0  # seconds between TTS chunks
        self.callback_budget = 10_000  # the budget the fake callback ranks its offers against
        self.orchestrators: list = []
        self.booked_any_time: list[bool] = []  # book()'s any_time, per booking
        self.judges: list = []  # Be the clinic: each JudgeLine the factory built


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
        def __init__(self, case, emit, max_clinics=3, receptionists=None) -> None:
            self.case, self.emit = case, emit
            self.receptionists = receptionists
            self.summary = None
            self.stepped_away = False
            w.orchestrators.append(self)
            w.max_clinics.append(max_clinics)
            w.preferences.append(case.preferences)

        async def start_callback(self):
            quotes = [Quote.model_validate(q) for q in json.loads(QUOTES.read_text(encoding="utf-8"))]
            prefs = DEMO_PREFERENCES.model_copy(update={"budget_max": w.callback_budget})
            self.case.offers = rank_offers(quotes, get_public_clinics(), prefs)
            self.case.status = CaseStatus.CALLBACK
            self.summary = SUMMARY
            await self.emit(CallbackStarted(case_id=self.case.id, summary=SUMMARY))
            return SUMMARY

        async def book(self, any_time=False):
            w.outcome.append("booked")
            w.booked_any_time.append(any_time)
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
            if self.receptionists is not None:
                return await clinic_run(self)
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

    async def clinic_run(orch):
        """Be the clinic, cut short: the judge picks up, Lana asks the price, the call ends."""
        clinic, sheet = next(pair for pair in load_demo_clinics() if pair[0].id == "parkway")
        case_id, call_id = orch.case.id, "parkway-judge1"

        async def line(speaker, text):
            await orch.emit(TranscriptFinal(case_id=case_id, call_id=call_id, speaker=speaker, text=text, t_seconds=0))

        async def say(text):
            await line(Speaker.AGENT, text)

        judge = orch.receptionists(clinic, sheet, say)
        w.judges.append(judge)
        for status in (CallStatus.DIALING, CallStatus.LIVE):
            await orch.emit(CallStatusEvent(case_id=case_id, call_id=call_id, clinic_id="parkway", status=status))
        greeting = await judge.answer()
        if greeting:
            await line(Speaker.CLINIC, greeting)
        await say(PRICE_QUESTION)
        reply = await judge.next_line(PRICE_QUESTION)
        await line(Speaker.CLINIC, reply)
        await orch.emit(CallStatusEvent(case_id=case_id, call_id=call_id, clinic_id="parkway", status=CallStatus.ENDED))
        orch.case.status = CaseStatus.OFFERS_READY
        await orch.emit(CaseStatusEvent(case_id=case_id, status=CaseStatus.OFFERS_READY, previous=CaseStatus.NEGOTIATING))
        return orch.case

    async def fake_speech(text, voice_id, *, speaker, model=None, stability=None):
        w.spoken.append((text, voice_id, speaker))
        if w.tts_error:
            raise w.tts_error
        for chunk in w.chunks:
            if w.chunk_delay:
                await asyncio.sleep(w.chunk_delay)
            yield chunk

    async def no_warm_up() -> None:
        return None

    async def fake_intake_json(prompt, schema, model, images=None, reasoning=None):
        if w.intake_delay:
            await asyncio.sleep(w.intake_delay)
        # The retry after a rejected line has no reply either, so the fixed question is said.
        retry = RETRY_PROMPT.split("{", 1)[0] in prompt
        reply = w.heard.pop(0) if w.heard and not retry else {}
        # A flat dict is just what the patient said; Lana's reply then falls back to the fixed question.
        known = {"updates", "notes", "answered", "asking", "reply", "done", "medical_question"}
        return schema.model_validate(reply if set(reply) & known else {"updates": reply})

    async def no_demo_intake(record, bus):
        return DEMO_PREFERENCES

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
        "voice_patient": "patient-voice",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(main, "Orchestrator", FakeOrchestrator)
    # Watch runs start straight from DEMO_PREFERENCES; the demo patient has its own tests below.
    monkeypatch.setattr(main, "demo_intake", no_demo_intake)
    monkeypatch.setattr(main, "_warm_up_voices", no_warm_up)
    monkeypatch.setattr(main, "_open_clients", lambda: None)
    monkeypatch.setattr(main, "_prewarm_lines", no_warm_up)
    monkeypatch.setattr(main, "StreamingSTT", lambda: FakeSTT(w))
    monkeypatch.setattr(main, "LINE_TAIL_S", 0)
    monkeypatch.setattr(settings, "end_grace_ms", 50)  # short grace period, so tests stay fast
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


def test_a_clinic_case_has_two_clinics_and_the_clinic_patient(client) -> None:
    case_id = _new_case(client, mode="clinic", clinics=3)
    record = store.get(case_id)
    assert record.clinics == 2
    assert record.case.mode == DemoMode.BE_THE_CLINIC
    assert record.case.preferences == main.clinic_patient().preferences
    assert main.clinic_patient().id not in [p.id for p in main.load_demo_patients()]  # never a Watch patient


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


def test_saying_be_the_clinic_tells_the_browser_to_start_clinic_mode(client, world) -> None:
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Let me be the clinic"})
        messages = _until(ws, lambda m: m["type"] == "command")
    assert messages[-1]["name"] == "clinic"
    assert world.runs == 0


def test_start_over_resets(client, world) -> None:
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Start over"})
        reset = _until(ws, lambda m: m["type"] == "command")
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


def test_callback_says_honestly_when_no_slot_is_in_the_patients_time(client, world) -> None:
    # Parkway wins the fake callback, and its openings are all evenings.
    world.heard = [{**FULL_INTAKE, "must_haves": [], "time_pref": "morning"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Braces near Dallas under 5k, monthly, mornings only"})
        said = _answered(ws)
        ws.send_json({"type": "user_text", "text": "Yes, book it"})
        messages = _until(ws, lambda m: m["type"] == "booked")
    question = main.NO_MATCH_QUESTION.format(times="mornings", slot="Monday, November 2 at 5:30 PM")
    assert said[0]["text"] == f"{SUMMARY} {question}"
    assert "They don't have mornings. The earliest is Monday, November 2 at 5:30 PM." in said[0]["text"]
    line = next(m for m in messages if m["type"] == "agent_said")
    assert line["text"].startswith("You're booked at Parkway Orthodontics for Monday, November 2 at 5:30 PM")
    assert world.booked_any_time == [True]  # booked only because the patient said yes to that time


def test_callback_with_a_slot_in_the_patients_time_asks_plainly(client, world) -> None:
    world.heard = [FULL_INTAKE]  # evenings, which Parkway has
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": FULL_REPLY})
        said = _answered(ws)
    assert said[0]["text"] == f"{SUMMARY} {main.BOOK_QUESTION}"


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


# ---------- no dead air, and no filler words ----------


def _second_turn_slow(ws, world, delay: float) -> list[dict]:
    """The first turn answered at once, then a second turn whose reply takes `delay` seconds."""
    world.heard = [{"treatment": "aligners"}, {"city": "Dallas"}]
    ws.send_json({"type": "user_text", "text": "Clear aligners"})
    _until(ws, lambda m: m["type"] == "agent_said")
    world.intake_delay = delay
    ws.send_json({"type": "user_text", "text": "Dallas"})
    return _until(ws, lambda m: m["type"] == "agent_said" and m["text"] == _question("budget"))


def test_a_slow_reply_speaks_no_filler(client, world) -> None:
    with _listen(client) as ws:
        _greeted(ws)
        messages = _second_turn_slow(ws, world, 2.0)
    kinds = [m["type"] for m in messages if m["type"] != "audio"]
    assert kinds == ["user_said", "intake", "agent_said"]  # only her real reply is spoken
    assert [t for t, _, s in world.spoken if s == Speaker.AGENT][-1] == _question("budget")


def test_a_fast_first_reply_is_her_question_only(client, world) -> None:
    world.heard = [{"treatment": "aligners"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": "Clear aligners"})
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    assert [m["text"] for m in messages if m["type"] == "agent_said"] == [_question("location")]


def test_no_filler_lines_exist_or_get_prewarmed() -> None:
    assert "fillers" not in Intake()._lines
    assert not hasattr(main, "FILLER_AFTER_S") and not hasattr(main.PatientSession, "_filler")


# ---------- Watch mode's demo patient ----------


def test_demo_patients_load_and_each_can_lead_to_a_hold() -> None:
    patients = main.load_demo_patients()
    assert len(patients) == 4 and len({p.id for p in patients}) == 4
    for patient in patients:
        assert set(patient.answers) <= set(TOPIC_FIELDS), patient.id
        # A hold needs a second clinic in range, at the default two clinics.
        assert len(pick_clinics(patient.preferences, 2)) == 2, patient.id
        for line in [patient.request, *patient.answers.values()]:
            assert "—" not in line
    assert {p.preferences.zip_code for p in patients} == {"76010", "75201", "76102", "75039"}
    assert set(main._demo_patient_lines()) >= {p.request for p in patients}


def _one_patient(monkeypatch, index: int = 0):
    patient = main.load_demo_patients()[index]
    monkeypatch.setattr(main, "demo_intake", REAL_DEMO_INTAKE)
    monkeypatch.setattr(main, "load_demo_patients", lambda: [patient])
    return patient


def test_watch_opens_with_the_demo_patient_and_lanas_real_intake(client, world, monkeypatch) -> None:
    patient = _one_patient(monkeypatch)
    reply = "Braces near Arlington in the evenings, got it. How would you like to pay?"
    world.heard = [
        {
            "updates": {"treatment": "braces", "city": "Arlington", "budget": "five thousand",
                        "must_haves": ["evening hours"]},
            "answered": ["treatment", "location", "budget", "must_haves"],
            "asking": "pay",
            "reply": reply,
        },
        {"updates": {"prefers_monthly": True}, "answered": ["pay"]},
    ]

    messages = _run(client)

    talk = [m for m in messages if m["type"] in ("user_said", "agent_said")]
    assert [(m["type"], m.get("voiced")) for m in talk] == [
        ("user_said", True), ("agent_said", None), ("user_said", True), ("agent_said", None),
    ]
    assert talk[0]["text"] == patient.request
    assert talk[1]["text"] == reply
    assert talk[2]["text"] == patient.answers["pay"]
    assert talk[3]["text"].startswith("Got it. Braces near Arlington, under $5,000, evenings.")
    # The patient is voiced in the patient voice, and the calls start only after Lana's line.
    assert [f["speaker"] for f in _audio(messages, talk[0]["seq"])] == ["patient"] * 3
    assert world.spoken[0] == (patient.request, "patient-voice", Speaker.PATIENT)
    first_status = next(i for i, m in enumerate(messages) if m["type"] == "case_status")
    assert messages.index(talk[3]) < first_status
    prefs = world.preferences[-1]
    assert prefs.zip_code == "76010" and prefs.budget_max == 5000
    assert prefs.prefers_monthly is True and prefs.must_haves == ["evening hours"]
    assert not any(m["type"] == "intake" for m in messages)  # agent ws-watch validates every event


def test_watch_uses_the_scenario_if_intake_fails(client, world, monkeypatch) -> None:
    patient = _one_patient(monkeypatch, 2)

    async def broken(*args, **kwargs):
        raise RuntimeError("Meta is down")

    monkeypatch.setattr(intake_module, "call_json", broken)

    messages = _run(client)

    said = [m["text"] for m in messages if m["type"] == "agent_said"]
    assert said == [main.DEMO_FALLBACK_CONFIRM]
    assert world.preferences[-1] == patient.preferences
    assert world.runs == 1


def test_stop_during_the_demo_intake_cancels_it(client, world, monkeypatch) -> None:
    _one_patient(monkeypatch)
    world.intake_delay = 30  # Lana is still thinking
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        _until(ws, lambda m: m["type"] == "audio" and m["last"])  # the patient's request
        ws.send_json({"type": "start"})
        assert ws.receive_json()["code"] == "running"
        ws.send_json({"type": "stop"})
        time.sleep(0.2)
    assert world.runs == 0


# ---------- grace period ----------


def test_two_finals_within_the_grace_period_are_one_turn(client, world) -> None:
    world.stt_script = [Final("I want braces"), Final("near Dallas")]
    world.heard = [{"treatment": "braces", "city": "Dallas"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    said = [m for m in messages if m["type"] == "user_said"]
    assert [m["text"] for m in said] == ["I want braces near Dallas"]
    assert messages[-1]["text"] == _question("budget")
    assert world.heard == []  # intake ran once


def test_talking_again_in_the_grace_period_holds_the_turn_open(client, world, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "end_grace_ms", 300)
    world.stt_script = [Final("I want braces"), Partial("near Dal"), Final("near Dallas")]
    world.heard = [{"treatment": "braces", "city": "Dallas"}]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    partial = next(m for m in messages if m["type"] == "user_partial")
    assert partial["text"] == "I want braces near Dal"  # the caption keeps what came before the pause
    assert [m["text"] for m in messages if m["type"] == "user_said"] == ["I want braces near Dallas"]


def test_the_grace_period_defaults_to_700_ms() -> None:
    assert type(get_settings()).model_fields["end_grace_ms"].default == 700


# ---------- barge-in ----------


def test_barge_in_cuts_lanas_line_and_the_next_final_is_a_turn(client, world) -> None:
    world.chunks = [b"a" * 10] * 20
    world.chunk_delay = 0.05  # her greeting takes about a second to send
    with _listen(client) as ws:
        ws.send_json({"type": "listen"})
        greeting = ws.receive_json()
        assert greeting["type"] == "agent_said"
        ws.receive_json()  # her first audio frame
        ws.send_json({"type": "barge_in"})
        frames = _until(ws, lambda m: m["type"] == "audio" and m["last"])
        last = frames[-1]
        assert last["line_seq"] == greeting["seq"] and last.get("cut") is True and last.get("failed") is True
        assert len(frames) < len(world.chunks)  # the rest of the line was never sent

        world.stt_script = [Final("Braces near Dallas")]
        world.heard = [{"treatment": "braces", "city": "Dallas"}]
        ws.send_bytes(LOUD)
        messages = _until(ws, lambda m: m["type"] == "agent_said")
    assert [m["text"] for m in messages if m["type"] == "user_said"] == ["Braces near Dallas"]
    assert messages[-1]["text"] == _question("budget")


def test_barge_in_lets_the_mic_through_while_her_line_still_plays(client, world, monkeypatch) -> None:
    monkeypatch.setattr(main, "LINE_TAIL_S", 30)  # her greeting is still playing in the browser
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_bytes(LOUD)
        time.sleep(0.2)
        assert world.stt_sent == []  # muted while she talks
        ws.send_json({"type": "barge_in"})
        time.sleep(0.05)
        ws.send_bytes(LOUD)
        _wait_for(lambda: LOUD in world.stt_sent)


def test_barge_in_is_ignored_during_the_calls(client, world) -> None:
    world.hang = True
    world.heard = [FULL_INTAKE]
    with _listen(client) as ws:
        _greeted(ws)
        ws.send_json({"type": "user_text", "text": FULL_REPLY})
        _until(ws, lambda m: m["type"] == "case_status")
        ws.send_json({"type": "barge_in"})
        ws.send_json({"type": "dance"})  # its error proves the barge-in before it was handled quietly
        messages = _until(ws, lambda m: m["type"] == "error")
    assert messages[-1]["code"] == "bad_message"
    assert not any(m["type"] == "audio" and m.get("cut") for m in messages)


# ---------- honest over-budget callback, step away ----------


def test_over_budget_callback_asks_to_book_it_anyway(client, world) -> None:
    world.callback_budget = 3000
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        said = _answered(ws)
        ws.send_json({"type": "user_text", "text": "Hold off"})
        messages = _until(ws, lambda m: m["type"] == "case_status" and m["status"] == "saved")
    assert said[0]["text"] == f"{SUMMARY} {main.BOOK_ANYWAY}"
    assert main.BOOK_ANYWAY == "Want me to book it anyway, or hold off?"
    assert next(m for m in messages if m["type"] == "agent_said")["text"] == main.SAVED_LINE
    assert world.classified == []  # "hold off" is a keyword now


def test_stepping_away_during_the_calls_is_told_to_the_orchestrator(client, world) -> None:
    world.hang = True
    case_id = _new_case(client)
    with client.websocket_connect(f"/ws/case/{case_id}") as ws:
        ws.send_json({"type": "start"})
        _until(ws, lambda m: m["type"] == "case_status")
        assert world.orchestrators[-1].stepped_away is False
        ws.send_json({"type": "command", "name": "step_away"})
        _wait_for(lambda: world.orchestrators[-1].stepped_away)


# ---------- Be the clinic ----------


def _clinic(client: TestClient):
    return client.websocket_connect(f"/ws/case/{_new_case(client, mode='clinic')}")


def _briefed(ws) -> list[dict]:
    ws.send_json({"type": "listen"})
    messages = _until(ws, lambda m: m["type"] == "audio" and m["last"])
    time.sleep(0.05)
    return messages


def _turn(ws, phase: str) -> list[dict]:
    return _until(ws, lambda m: m["type"] == "judge_turn_start" and m["phase"] == phase)


def test_clinic_mode_starts_with_the_briefing_and_waits_for_ready(client, world) -> None:
    with _clinic(client) as ws:
        said = _briefed(ws)
        ws.send_json({"type": "user_text", "text": "What do I do?"})
        nudge = _until(ws, lambda m: m["type"] == "agent_said")
        assert world.runs == 0
        ws.send_json({"type": "user_text", "text": "Ready"})
        _turn(ws, "greeting")
    assert said[0]["type"] == "agent_said" and said[0]["text"] == main.BRIEFING
    assert nudge[-1]["text"] == main.READY_AGAIN
    assert world.runs == 1 and world.max_clinics == [2]
    assert world.preferences[-1] == main.clinic_patient().preferences


def test_clinic_mode_turns_typed_replies_into_the_judges_lines(client, world) -> None:
    with _clinic(client) as ws:
        _briefed(ws)
        ws.send_json({"type": "user_text", "text": "Yes"})
        _turn(ws, "greeting")
        ws.send_json({"type": "user_text", "text": "Parkway Orthodontics, how can I help?"})
        first = _turn(ws, "price")
        ws.send_json({"type": "user_text", "text": "It's $4,800 plus a $250 records fee."})
        messages = _until(ws, lambda m: m["type"] == "judge_turn_start" and m["phase"] == "wrap")
        messages += _until(ws, lambda m: m["type"] == "case_status" and m["status"] == "offers_ready")
    messages = first + messages
    judge = [m for m in messages if m["type"] == "transcript_final" and m.get("judge")]
    assert [m["text"] for m in judge] == ["Parkway Orthodontics, how can I help?", "It's $4,800 plus a $250 records fee."]
    assert all(m["speaker"] == "clinic" for m in judge)
    assert not any(_audio(messages, m["seq"]) for m in judge)  # the judge's own words are never voiced
    assert not any(m["type"] == "user_said" for m in messages)
    phases = [m["phase"] for m in messages if m["type"] == "judge_turn_start"]
    assert phases == ["price", "wrap"]  # the wrap hint follows the end of the call
    assert not any(m["type"] == "callback_started" for m in messages)
    time.sleep(0.1)
    assert world.orchestrators[-1].summary is None  # no callback in clinic mode


def test_barge_in_works_on_lanas_line_to_the_judge(client, world) -> None:
    with _clinic(client) as ws:
        _briefed(ws)
        ws.send_json({"type": "user_text", "text": "Ready"})
        _turn(ws, "greeting")
        world.chunks = [b"a" * 10] * 20
        world.chunk_delay = 0.05  # her price question takes about a second to send
        ws.send_json({"type": "user_text", "text": "Parkway, hi!"})
        question = _until(ws, lambda m: m["type"] == "transcript_final" and m["speaker"] == "agent")[-1]
        ws.receive_json()  # her first audio frame
        ws.send_json({"type": "barge_in"})
        frames = _until(ws, lambda m: m["type"] == "audio" and m["last"])
        ws.send_json({"type": "user_text", "text": "It's $4,800 all in."})
        messages = _until(ws, lambda m: m["type"] == "transcript_final" and m.get("judge") and "4,800" in m["text"])
    assert frames[-1]["line_seq"] == question["seq"] and frames[-1].get("cut") is True
    assert len(frames) < len(world.chunks)
    assert messages[-1]["text"] == "It's $4,800 all in."


def test_a_clinic_case_does_not_open_with_start(client, world) -> None:
    with _clinic(client) as ws:
        ws.send_json({"type": "start"})
        msg = ws.receive_json()
    assert msg["type"] == "error" and msg["code"] == "bad_message"
    assert world.runs == 0
