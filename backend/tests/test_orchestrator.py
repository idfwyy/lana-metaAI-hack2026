"""Orchestrator and researcher with the caller, simulator and LLM mocked. No network."""

import pytest

from app.agent import negotiator, orchestrator
from app.agent.caller import CLOSING_LINE, DISCLOSURE, HOLD_REQUEST, WORDS_PER_SECOND, CallResult
from app.agent.extractor import Transcript, TranscriptLine
from app.agent.orchestrator import (
    NO_DEAL_LINE,
    RESUME_CLOSE_LINE,
    RESUME_LINE,
    IllegalTransition,
    Orchestrator,
)
from app.agent.researcher import pick_clinics
from app.clinics.simulator import is_hold_request
from app.config import get_settings
from app.schemas.case import Case
from app.schemas.checklist import ClinicChecklist, SourcedAnswer
from app.schemas.enums import CallStatus, CaseStatus, Speaker, Treatment
from app.schemas.events import (
    CallStatusEvent,
    CaseStatusEvent,
    ConcessionWon,
    ErrorEvent,
    OffersReady,
    TranscriptFinal,
)
from app.schemas.preferences import Preferences

S = CaseStatus


def _prefs(**overrides) -> Preferences:
    data = dict(
        treatment=Treatment.BRACES,
        zip_code="76010",
        max_distance_mi=15,
        budget_max=5000,
        pay_in_full_ok=True,
        uses_hsa_fsa=True,
        prefers_monthly=True,
        must_haves=["evening hours"],
    )
    return Preferences(**{**data, **overrides})


DISCLOSURE_TEXT = DISCLOSURE.format(agent_name=get_settings().agent_name)
PRICE_QUESTION = "How much is the full treatment?"


class World:
    """Scripted clinics: prices per clinic, negotiation replies, readings, and failures."""

    def __init__(self) -> None:
        # clinic_id -> (treatment price, records fee, evening hours)
        self.prices = {
            "parkway": (4800, 250, True),
            "sunrise": (4200, 0, False),
            "lakeside": (4500, 150, False),
        }
        self.replies: dict[str, list[str]] = {}
        self.readings: list[dict] = []
        self.fail_on: set[str] = set()
        self.sims: dict[str, list["FakeSim"]] = {}
        self.callers: list["FakeCaller"] = []

    def price_reply(self, clinic_id: str) -> str:
        total, fee, evening = self.prices[clinic_id]
        hours = "We do have evening appointments." if evening else "No evening appointments."
        return f"It's ${total:,} and the records fee is ${fee:,}. {hours}"


def _answer(value, evidence: str, call_id: str, t: float) -> SourcedAnswer:
    return SourcedAnswer(value=value, speaker=Speaker.CLINIC, evidence=evidence, call_id=call_id, t_seconds=t)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    class FakeSim:
        def __init__(self, clinic, sheet) -> None:
            self.clinic = clinic
            self.heard: list[str] = []
            w.sims.setdefault(clinic.id, []).append(self)

        async def next_line(self, lana_line: str) -> str:
            self.heard.append(lana_line)
            if is_hold_request(lana_line):
                return "Sure, I'll hold."
            if PRICE_QUESTION in lana_line:
                return w.price_reply(self.clinic.id)
            queue = w.replies.get(self.clinic.id, [])
            return queue.pop(0) if queue else "Sorry, we can't do that."

    class FakeCaller:
        def __init__(
            self, clinic, preferences, on_line, call_id=None, end_with="close", on_checklist=None
        ) -> None:
            self.clinic = clinic
            self.on_line = on_line
            self.call_id = call_id or "fake"
            self.end_with = end_with
            self.on_checklist = on_checklist
            self.transcript = Transcript(call_id=self.call_id, clinic_id=clinic.id)
            self.clock = 0.0
            w.callers.append(self)

        async def _say(self, speaker: Speaker, text: str) -> float:
            t = round(self.clock, 1)
            self.clock += len(text.split()) / WORDS_PER_SECOND
            self.transcript.lines.append(TranscriptLine(speaker=speaker, text=text, t_seconds=t))
            await self.on_line(speaker, text, t)
            return t

        async def run(self, receptionist) -> CallResult:
            if self.clinic.id in w.fail_on:
                raise RuntimeError("simulated network failure")
            line = f"{DISCLOSURE_TEXT} {PRICE_QUESTION}"
            await self._say(Speaker.AGENT, line)
            reply = await receptionist.next_line(line)
            t = await self._say(Speaker.CLINIC, reply)
            total, fee, evening = w.prices[self.clinic.id]
            checklist = ClinicChecklist(
                total_price=_answer(total, f"${total:,}", self.call_id, t),
                records_fee=_answer(fee, f"${fee:,}", self.call_id, t),
                evening_hours=_answer(evening, "evening appointments", self.call_id, t),
            )
            await self.on_checklist(checklist)
            if self.end_with == "hold":
                await self._say(Speaker.AGENT, HOLD_REQUEST)
                await self._say(Speaker.CLINIC, await receptionist.next_line(HOLD_REQUEST))
            else:
                await self._say(Speaker.AGENT, CLOSING_LINE)
            return CallResult(transcript=self.transcript, checklist=checklist)

    async def broken_chat(messages, model, reasoning=None):
        raise RuntimeError("no network in tests")
        yield  # pragma: no cover

    async def fake_json(prompt, schema, model, images=None, reasoning=None):
        return schema.model_validate(w.readings.pop(0) if w.readings else {"verdict": "no"})

    async def same_offers(offers):
        return offers

    monkeypatch.setattr(orchestrator, "Caller", FakeCaller)
    monkeypatch.setattr(orchestrator, "ClinicSimulator", FakeSim)
    monkeypatch.setattr(orchestrator, "add_tradeoffs", same_offers)
    # Phrasing falls back to the fixed templates; replies are read from the script.
    monkeypatch.setattr(negotiator, "stream_chat", broken_chat)
    monkeypatch.setattr(negotiator, "call_json", fake_json)
    return w


def _win_on_parkway(world: World) -> None:
    world.replies["parkway"] = [
        "Good news, we can match $4,200.",
        "We can do $210 off if paid in full.",
        "Sorry, no extra retainers.",
    ]
    world.readings = [
        {"verdict": "yes", "new_all_in": 4200, "evidence": "we can match $4,200"},
        {"verdict": "yes", "amount_off": 210, "evidence": "$210 off"},
        {"verdict": "no", "evidence": "no extra retainers"},
    ]


async def _run(prefs: Preferences | None = None, max_clinics: int = 3) -> tuple[Case, list]:
    events: list = []
    case = Case(preferences=prefs or _prefs())
    await Orchestrator(case, events.append, max_clinics=max_clinics).run()
    return case, events


def _statuses(events) -> list[CaseStatus]:
    return [e.status for e in events if isinstance(e, CaseStatusEvent)]


def _call_ids(events, clinic_id: str) -> list[str]:
    ids: list[str] = []
    for e in events:
        if isinstance(e, CallStatusEvent) and e.clinic_id == clinic_id and e.call_id not in ids:
            ids.append(e.call_id)
    return ids


def _lines(events, call_id: str) -> list[TranscriptFinal]:
    return [e for e in events if isinstance(e, TranscriptFinal) and e.call_id == call_id]


def _index(events, predicate) -> list[int]:
    return [i for i, e in enumerate(events) if predicate(e)]


# ---------- researcher ----------


def test_researcher_orders_by_fit_then_rating_and_logs() -> None:
    logs: list[str] = []

    picked = pick_clinics(_prefs(), on_log=logs.append)

    assert [c.id for c in picked] == ["parkway", "sunrise", "lakeside"]
    assert logs == [
        "Found 3 orthodontists within 15 miles",
        "Calling Parkway first: best fit for evening hours",
        "Then Sunrise, then Lakeside",
    ]


def test_researcher_filters_distance_and_truncates() -> None:
    assert [c.id for c in pick_clinics(_prefs(max_distance_mi=6))] == ["parkway", "sunrise"]
    assert [c.id for c in pick_clinics(_prefs(), max_clinics=2)] == ["parkway", "sunrise"]
    logs: list[str] = []
    assert pick_clinics(_prefs(max_distance_mi=1), on_log=logs.append) == []
    assert logs == ["No orthodontists found within 1 mile"]


# ---------- states ----------


async def test_legal_path_three_clinics(world: World) -> None:
    _win_on_parkway(world)

    case, events = await _run()

    assert _statuses(events) == [
        S.INTAKE, S.RESEARCH, S.CALLING, S.HOLDING, S.CALLING, S.CALLING, S.NEGOTIATING, S.OFFERS_READY,
    ]
    assert case.status == S.OFFERS_READY
    assert [c.id for c in case.clinics] == ["parkway", "sunrise", "lakeside"]
    offers = [e for e in events if isinstance(e, OffersReady)]
    assert len(offers) == 1 and offers[0].offers == case.offers
    assert case.held_call_id is None
    assert case.error is None


async def test_async_emit_is_awaited(world: World) -> None:
    seen: list = []

    async def emit(event) -> None:
        seen.append(event)

    case = Case(preferences=_prefs())
    await Orchestrator(case, emit, max_clinics=2).run()

    assert case.status == S.OFFERS_READY
    assert _statuses(seen)[-1] == S.OFFERS_READY


async def test_illegal_move_raises() -> None:
    case = Case(preferences=_prefs())
    orch = Orchestrator(case, lambda e: None)

    with pytest.raises(IllegalTransition):
        await orch.advance(S.CALLING)
    assert case.status == S.NEW

    case.status = S.CALLING
    with pytest.raises(IllegalTransition):
        await orch.advance(S.NEGOTIATING)  # only allowed while a call is on hold
    assert case.status == S.CALLING

    with pytest.raises(ValueError):
        Orchestrator(case, lambda e: None, max_clinics=1)


# ---------- hold and resume ----------


async def test_hold_after_clinic_one_and_resume_after_the_others(world: World) -> None:
    _win_on_parkway(world)

    case, events = await _run()

    parkway = _call_ids(events, "parkway")
    assert len(parkway) == 1  # the resumed call is the same call
    call_id = parkway[0]
    on_hold = _index(events, lambda e: isinstance(e, CallStatusEvent) and e.status == CallStatus.ON_HOLD)
    assert len(on_hold) == 1 and events[on_hold[0]].call_id == call_id

    other_ids = _call_ids(events, "sunrise") + _call_ids(events, "lakeside")
    other_lines = _index(events, lambda e: isinstance(e, TranscriptFinal) and e.call_id in other_ids)
    others_ended = _index(
        events,
        lambda e: isinstance(e, CallStatusEvent) and e.call_id in other_ids and e.status == CallStatus.ENDED,
    )
    parkway_lines = _index(events, lambda e: isinstance(e, TranscriptFinal) and e.call_id == call_id)
    before = [i for i in parkway_lines if i < on_hold[0]]
    after = [i for i in parkway_lines if i > on_hold[0]]

    assert events[before[-2]].text == HOLD_REQUEST
    assert on_hold[0] < min(other_lines)
    assert max(others_ended) < min(after)
    assert events[after[0]].text.startswith(f"{RESUME_LINE} ")

    # The resumed call keeps its own clock from where it paused.
    last = events[before[-1]]
    paused_at = last.t_seconds + len(last.text.split()) / WORDS_PER_SECOND
    assert events[after[0]].t_seconds == round(paused_at, 1)


async def test_resumed_call_has_no_second_disclosure(world: World) -> None:
    _win_on_parkway(world)

    case, events = await _run()

    call_id = _call_ids(events, "parkway")[0]
    lana = [e.text for e in _lines(events, call_id) if e.speaker == Speaker.AGENT]
    assert sum(DISCLOSURE_TEXT in line for line in lana) == 1
    assert lana[0].startswith(DISCLOSURE_TEXT)
    # Same receptionist the whole call, so it heard the hold and the resume.
    assert len(world.sims["parkway"]) == 1
    heard = world.sims["parkway"][0].heard
    assert HOLD_REQUEST in heard
    assert heard[heard.index(HOLD_REQUEST) + 1].startswith(RESUME_LINE)


async def test_no_hold_with_one_clinic(world: World) -> None:
    case, events = await _run(_prefs(max_distance_mi=4))

    assert [c.id for c in case.clinics] == ["parkway"]
    assert _statuses(events) == [S.INTAKE, S.RESEARCH, S.CALLING, S.QUOTES_IN, S.OFFERS_READY]
    assert not any(isinstance(e, CallStatusEvent) and e.status == CallStatus.ON_HOLD for e in events)
    assert not any(isinstance(e, TranscriptFinal) and e.text == HOLD_REQUEST for e in events)
    assert world.callers[0].end_with == "close"
    assert case.negotiation is None


# ---------- negotiation ----------


async def test_no_negotiation_when_clinic_one_is_cheapest(world: World) -> None:
    world.prices["parkway"] = (4000, 0, True)

    case, events = await _run()

    assert case.status == S.OFFERS_READY
    assert case.negotiation is None
    assert not any(isinstance(e, ConcessionWon) for e in events)
    call_id = _call_ids(events, "parkway")[0]
    assert _lines(events, call_id)[-1].text == NO_DEAL_LINE
    assert world.sims["parkway"][0].heard[-1] == HOLD_REQUEST  # nothing was asked after the hold
    assert S.NEGOTIATING in _statuses(events)


async def test_other_target_gets_a_fresh_call_with_disclosure(world: World) -> None:
    world.prices["parkway"] = (4800, 250, False)
    world.prices["sunrise"] = (4900, 0, True)

    case, events = await _run()

    held_id = _call_ids(events, "parkway")[0]
    assert _lines(events, held_id)[-1].text == RESUME_CLOSE_LINE
    sunrise_ids = _call_ids(events, "sunrise")
    assert len(sunrise_ids) == 2
    fresh = _lines(events, sunrise_ids[1])
    assert fresh[0].text.startswith(DISCLOSURE_TEXT)
    assert fresh[0].t_seconds == 0.0
    assert len(world.sims["sunrise"]) == 2
    assert case.negotiation is not None and case.negotiation.clinic_id == "sunrise"


async def test_total_saved_equals_first_minus_final(world: World) -> None:
    _win_on_parkway(world)

    case, events = await _run()

    deal = case.negotiation
    assert deal is not None and deal.clinic_id == "parkway"
    assert deal.first_all_in == 5050
    assert deal.final_all_in == 3990
    won = [e for e in events if isinstance(e, ConcessionWon)]
    assert [e.concession.label for e in won] == ["Price match", "Pay-in-full discount"]
    assert [e.new_all_in for e in won] == [4200, 3990]
    assert sum(e.concession.amount for e in won) == deal.first_all_in - deal.final_all_in == deal.total_saved
    # Evidence seconds are on the resumed call's clock.
    call_id = _call_ids(events, "parkway")[0]
    clinic_lines = {e.t_seconds: e.text for e in _lines(events, call_id) if e.speaker == Speaker.CLINIC}
    for c in deal.concessions:
        assert c.evidence.call_id == call_id
        assert c.evidence.evidence in clinic_lines[c.evidence.t_seconds]
    top = case.offers[0]
    assert top.clinic.id == "parkway" and top.negotiation == deal


# ---------- errors ----------


async def test_exception_moves_case_to_error(world: World) -> None:
    world.fail_on.add("sunrise")

    case, events = await _run()

    assert case.status == S.ERROR
    assert _statuses(events)[-1] == S.ERROR
    assert isinstance(events[-1], ErrorEvent)
    assert "Sunrise Orthodontic Studio" in events[-1].message
    assert "Traceback" not in events[-1].message
    assert case.error == events[-1].message
    assert not any(isinstance(e, OffersReady) for e in events)
