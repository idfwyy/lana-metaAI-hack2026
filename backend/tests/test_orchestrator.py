"""Orchestrator and researcher with the caller, simulator and LLM mocked. No network."""

import asyncio
from datetime import datetime

import pytest

from app.agent import negotiator, orchestrator
from app.agent.caller import CLOSING_LINE, DISCLOSURE, HOLD_REQUEST, WORDS_PER_SECOND, CallEnded, CallResult
from app.agent.extractor import Transcript, TranscriptLine
from app.agent.orchestrator import (
    NO_DEAL_LINE,
    RESUME_CLOSE_LINE,
    RESUME_LINE,
    IllegalTransition,
    NoMatchingSlot,
    Orchestrator,
    _summary_facts,
    pick_slot,
    spoken_slot,
    summary_problem,
    template_summary,
    write_summary,
)
from app.agent.negotiator import CLOSING_LINE as NEGOTIATION_CLOSING
from app.agent.researcher import pick_clinics
from app.clinics.judge_line import CALL_BACK_LATER, JudgeLine
from app.clinics.simulator import is_hold_request
from app.config import get_settings
from app.schemas.case import Case
from app.schemas.checklist import ClinicChecklist, SourcedAnswer
from app.schemas.enums import CallStatus, CaseStatus, Speaker, Treatment
from app.schemas.events import (
    Booked,
    CallbackStarted,
    CallStatusEvent,
    CaseStatusEvent,
    ConcessionWon,
    ErrorEvent,
    LogLine,
    OffersReady,
    TranscriptFinal,
)
from app.schemas.preferences import Preferences

S = CaseStatus
TZ = get_settings().tz


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
            try:
                reply = await receptionist.next_line(line)
            except CallEnded:  # like the real Caller: the call stops with nothing confirmed
                return CallResult(transcript=self.transcript, checklist=ClinicChecklist(), ended_early=True)
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
                reply = await receptionist.next_line(HOLD_REQUEST)
                if reply:  # like the real Caller: a silent hold says nothing
                    await self._say(Speaker.CLINIC, reply)
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
    # The callback summary falls back to its template.
    monkeypatch.setattr(orchestrator, "stream_chat", broken_chat)
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
        "Found 3 orthodontists within 15 miles of Arlington",
        "Calling Parkway first, they fit best (evening hours, 4.9 stars)",
        "Then Sunrise, then Lakeside",
    ]


def test_researcher_reason_without_must_haves_is_rating_and_unknown_zip_is_shown() -> None:
    logs: list[str] = []

    pick_clinics(_prefs(must_haves=[], zip_code="75999"), max_clinics=2, on_log=logs.append)

    assert logs == [
        "Found 3 orthodontists within 15 miles of 75999",
        "Calling Parkway first, they're the highest rated (4.9 stars, 3.1 mi away)",
        "Then Sunrise",
    ]


def test_researcher_filters_distance_and_truncates() -> None:
    assert [c.id for c in pick_clinics(_prefs(max_distance_mi=6))] == ["parkway", "sunrise"]
    assert [c.id for c in pick_clinics(_prefs(), max_clinics=2)] == ["parkway", "sunrise"]
    logs: list[str] = []
    assert pick_clinics(_prefs(max_distance_mi=1), on_log=logs.append) == []
    assert logs == ["No orthodontists found within 1 mile of Arlington"]


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


# ---------- live price drops ----------


async def test_concession_won_is_emitted_during_the_negotiation_call(world: World) -> None:
    _win_on_parkway(world)

    case, events = await _run()

    call_id = _call_ids(events, "parkway")[0]
    won = _index(events, lambda e: isinstance(e, ConcessionWon))
    ended = _index(
        events, lambda e: isinstance(e, CallStatusEvent) and e.call_id == call_id and e.status == CallStatus.ENDED
    )
    closing = _index(events, lambda e: isinstance(e, TranscriptFinal) and e.text == NEGOTIATION_CLOSING)
    pay_in_full_ask = _index(
        events, lambda e: isinstance(e, TranscriptFinal) and e.speaker == Speaker.AGENT and "pays in full" in e.text
    )
    assert len(won) == 2
    # The match drops the price before Lana even asks for the pay-in-full discount.
    assert won[0] < pay_in_full_ask[0] < won[1]
    assert won[-1] < closing[0] < ended[-1]


# ---------- the callback ----------


def _with_slots(case: Case) -> None:
    quote = case.offers[0].quote
    slots = [datetime(2026, 11, 4, 18, 0, tzinfo=TZ), datetime(2026, 11, 3, 17, 30, tzinfo=TZ)]
    quote.checklist.next_slots = _answer(slots, "Tuesday at 5:30", "c1", 3.0)


async def _ready(world: World) -> tuple[Orchestrator, list]:
    _win_on_parkway(world)
    events: list = []
    orch = Orchestrator(Case(preferences=_prefs()), events.append)
    await orch.run()
    return orch, events


async def test_summary_with_a_made_up_number_falls_back_to_the_template(world: World, monkeypatch) -> None:
    orch, _ = await _ready(world)

    async def chat(messages, model, reasoning=None):
        yield "Parkway Orthodontics came in at $3,990 all in, so you save $9,999."

    monkeypatch.setattr(orchestrator, "stream_chat", chat)
    summary = await write_summary(orch.case)

    assert "$9,999" not in summary
    assert summary == template_summary(orch.case)
    assert summary.startswith("Parkway Orthodontics came in at $3,990 all in.")
    assert "$1,060" in summary  # saved: 5,050 - 3,990


async def test_clean_summary_is_kept(world: World, monkeypatch) -> None:
    orch, _ = await _ready(world)
    clean = "Hi, it's Lana. Parkway Orthodontics came down from $5,050 to $3,990 all in, a $1,060 saving."

    async def chat(messages, model, reasoning=None):
        yield clean

    monkeypatch.setattr(orchestrator, "stream_chat", chat)
    assert await write_summary(orch.case) == clean


async def test_start_callback_rings_with_the_summary(world: World) -> None:
    orch, events = await _ready(world)

    summary = await orch.start_callback()

    assert orch.case.status == S.CALLBACK and orch.summary == summary
    assert isinstance(events[-1], CallbackStarted) and events[-1].summary == summary
    assert summary == template_summary(orch.case)  # the model is broken in these tests


async def test_yes_books_the_first_open_slot(world: World) -> None:
    orch, events = await _ready(world)
    await orch.start_callback()
    _with_slots(orch.case)

    booked = await orch.book()

    assert booked is not None and isinstance(events[-1], Booked)
    assert booked.clinic_id == "parkway"
    assert booked.slot == datetime(2026, 11, 3, 17, 30, tzinfo=TZ)
    assert booked.total_saved == 1060
    assert orch.case.status == S.BOOKED
    assert spoken_slot(booked.slot) == "Tuesday, November 3 at 5:30 PM"


async def test_no_slot_means_no_booking(world: World) -> None:
    orch, _ = await _ready(world)
    await orch.start_callback()

    assert await orch.book() is None
    assert orch.case.status == S.CALLBACK


async def test_not_yet_saves(world: World) -> None:
    orch, events = await _ready(world)
    await orch.start_callback()

    await orch.save()

    assert orch.case.status == S.SAVED
    assert _statuses(events)[-1] == S.SAVED


async def test_log_reads_like_lana_thinking(world: World) -> None:
    _win_on_parkway(world)

    _, events = await _run()

    logs = [e.text for e in events if isinstance(e, LogLine)]
    assert logs == [
        "Found 3 orthodontists within 15 miles of Arlington",
        "Calling Parkway first, they fit best (evening hours, 4.9 stars)",
        "Then Sunrise, then Lakeside",
        "Parkway is $5,050 all in. That's the best fit so far, so I'm asking them to hold while I check Sunrise and Lakeside",
        "Sunrise is $4,200 all in, $850 less than Parkway",
        "Lakeside is $4,650 all in, $400 less than Parkway",
        "Going back to Parkway to ask them to match Sunrise's $4,200",
        "Parkway came down to $3,990, $1,060 off",
    ]
    assert len(set(logs)) == len(logs)  # nothing repeated per clinic


# ---------- honest over-budget results, any city, step away ----------


async def _ready_with(world: World, **prefs) -> Orchestrator:
    _win_on_parkway(world)
    orch = Orchestrator(Case(preferences=_prefs(**prefs)), lambda e: None)
    await orch.run()
    return orch


async def test_no_offer_within_budget_summary_names_the_overage(world: World) -> None:
    orch = await _ready_with(world, budget_max=3000)

    top = orch.case.offers[0]
    assert top.clinic.id == "parkway" and top.over_budget_by == 990  # $3,990 against $3,000
    summary = template_summary(orch.case)
    assert summary.startswith(
        "Nobody would go under $3,000. The best I got was $3,990 at Parkway Orthodontics, $990 over your budget."
    )
    assert summary_problem(orch.case, summary) is None


async def test_the_model_may_say_the_budget_and_the_overage(world: World, monkeypatch) -> None:
    orch = await _ready_with(world, budget_max=3000)
    line = "Nobody would go under $3,000. The best I got was $3,990 at Parkway, $990 over your budget."
    prompts: list[str] = []

    async def chat(messages, model, reasoning=None):
        prompts.append(messages[0]["content"])
        yield line

    monkeypatch.setattr(orchestrator, "stream_chat", chat)

    assert await write_summary(orch.case) == line
    assert "No clinic came in under the patient's budget" in prompts[0]
    assert "Best clinic is over budget by: $990" in prompts[0]


async def test_within_budget_summary_is_unchanged(world: World) -> None:
    orch = await _ready_with(world, budget_max=5000)

    assert orch.case.offers[0].over_budget_by == 0
    assert template_summary(orch.case).startswith("Parkway Orthodontics came in at $3,990 all in.")


async def test_step_away_summary_starts_with_the_call_count(world: World, monkeypatch) -> None:
    orch = await _ready_with(world)
    assert orch.calls_made == 3  # three clinics; resuming Parkway's held call is not a new call

    async def chat(messages, model, reasoning=None):
        yield "Hi, it's Lana. Parkway Orthodontics came down from $5,050 to $3,990 all in."

    monkeypatch.setattr(orchestrator, "stream_chat", chat)
    orch.stepped_away = True

    summary = await orch.start_callback()

    assert summary == "I made 3 calls while you were away. Parkway Orthodontics came down from $5,050 to $3,990 all in."


async def test_no_step_away_no_call_count(world: World) -> None:
    orch = await _ready_with(world)

    assert not (await orch.start_callback()).startswith("I made")


async def test_two_clinics_make_two_calls(world: World) -> None:
    _win_on_parkway(world)
    orch = Orchestrator(Case(preferences=_prefs()), lambda e: None, max_clinics=2)
    await orch.run()
    orch.stepped_away = True

    assert (await orch.start_callback()).startswith("I made 2 calls while you were away.")


def test_researcher_labels_demo_clinics_near_an_outside_place() -> None:
    logs: list[str] = []

    picked = pick_clinics(_prefs(place="San Francisco"), on_log=logs.append)

    assert [c.id for c in picked] == ["parkway", "sunrise", "lakeside"]  # the same demo clinics
    assert logs[0] == "Found 3 demo clinics near San Francisco"


async def test_summary_facts_say_the_clinics_are_demo_ones_near_the_place(world: World, monkeypatch) -> None:
    orch = await _ready_with(world, place="San Francisco")
    prompts: list[str] = []

    async def chat(messages, model, reasoning=None):
        prompts.append(messages[0]["content"])
        yield "Parkway Orthodontics came in at $3,990 all in."

    monkeypatch.setattr(orchestrator, "stream_chat", chat)
    await write_summary(orch.case)

    assert "demo clinics placed near San Francisco, not real clinics there" in prompts[0]


# ---------- the patient's preferred time ----------


def test_researcher_prefers_a_morning_clinic_for_a_morning_patient() -> None:
    logs: list[str] = []

    picked = pick_clinics(_prefs(must_haves=[], time_pref="morning"), on_log=logs.append)

    assert picked[0].id == "sunrise"
    assert logs[1] == "Calling Sunrise first, they have morning openings and 4.6 stars"


def test_researcher_reads_morning_hours_as_a_must_have() -> None:
    picked = pick_clinics(_prefs(must_haves=["morning hours"], time_pref="morning"))
    assert picked[0].id == "sunrise"


def test_researcher_weekend_patient_calls_the_saturday_clinic_first() -> None:
    logs: list[str] = []
    picked = pick_clinics(_prefs(must_haves=[], time_pref="weekend"), on_log=logs.append)
    assert picked[0].id == "lakeside"
    assert "weekend openings" in logs[1]


MONDAY_MORNING = datetime(2026, 11, 2, 9, 0, tzinfo=TZ)
TUESDAY_EVENING = datetime(2026, 11, 3, 17, 30, tzinfo=TZ)
WEDNESDAY_EVENING = datetime(2026, 11, 4, 18, 0, tzinfo=TZ)


def _slots(case: Case, *slots: datetime) -> None:
    case.offers[0].quote.checklist.next_slots = _answer(list(slots), "Tuesday at 5:30", "c1", 3.0)


async def test_book_picks_the_morning_slot_when_one_exists(world: World) -> None:
    orch, _ = await _ready(world)
    orch.case.preferences = orch.case.preferences.model_copy(update={"time_pref": "morning"})
    await orch.start_callback()
    _slots(orch.case, TUESDAY_EVENING, MONDAY_MORNING, WEDNESDAY_EVENING)

    booked = await orch.book()

    assert isinstance(booked, Booked)
    assert booked.slot == MONDAY_MORNING  # not the earlier-listed evening one


async def test_book_with_no_morning_slot_says_so_and_books_nothing(world: World) -> None:
    orch, _ = await _ready(world)
    orch.case.preferences = orch.case.preferences.model_copy(update={"time_pref": "morning"})
    await orch.start_callback()
    _slots(orch.case, WEDNESDAY_EVENING, TUESDAY_EVENING)

    result = await orch.book()

    assert result == NoMatchingSlot(clinic_id="parkway", time_pref="morning", earliest=TUESDAY_EVENING)
    assert orch.case.status == S.CALLBACK

    booked = await orch.book(any_time=True)  # the patient said yes to that time
    assert isinstance(booked, Booked) and booked.slot == TUESDAY_EVENING
    assert orch.case.status == S.BOOKED


async def test_pick_slot_any_time_is_the_earliest(world: World) -> None:
    orch, _ = await _ready(world)
    _slots(orch.case, WEDNESDAY_EVENING, MONDAY_MORNING)
    assert pick_slot(orch.case.offers[0]) == (MONDAY_MORNING, True)
    assert pick_slot(orch.case.offers[0], "weekend") == (MONDAY_MORNING, False)


async def test_summary_facts_name_the_preferred_time_slot_and_the_note_answer(world: World) -> None:
    orch, _ = await _ready(world)
    orch.case.preferences = orch.case.preferences.model_copy(update={"time_pref": "morning"})
    _slots(orch.case, TUESDAY_EVENING, MONDAY_MORNING)
    checklist = orch.case.offers[0].quote.checklist
    checklist.note_question = "Does anyone at your office speak Spanish?"
    checklist.note_answer = _answer("Yes, two assistants speak Spanish", "two of our assistants speak Spanish", "c1", 9.0)

    facts = _summary_facts(orch.case)

    assert "- Next opening in the patient's preferred time (mornings): Monday, November 2 at 9:00 AM" in facts
    assert "Tuesday" not in facts
    assert "- The clinic said: Yes, two assistants speak Spanish" in facts


async def test_summary_leaves_out_a_slot_outside_the_preferred_time(world: World) -> None:
    orch, _ = await _ready(world)
    orch.case.preferences = orch.case.preferences.model_copy(update={"time_pref": "morning"})
    _slots(orch.case, TUESDAY_EVENING)

    assert "Their next opening" not in template_summary(orch.case)
    assert "No opening in the patient's preferred time (mornings)" in _summary_facts(orch.case)
    assert "Not confirmed" not in _summary_facts(orch.case)
    assert "The clinic said" not in _summary_facts(orch.case)  # nothing invented without an answer


# ---------- Be the clinic: a judge plays Parkway ----------


class ScriptedJudge:
    """A receptionist factory: Parkway is a JudgeLine answered from `replies` (None = silence)."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.phases: list[str] = []
        self.judges: list[JudgeLine] = []
        self.built: dict[str, str] = {}

    def __call__(self, clinic, sheet, say):
        if clinic.id != "parkway":
            receptionist = orchestrator.simulated(clinic, sheet, say)
        else:
            async def turn_start(phase):
                self.phases.append(phase)
                reply = self.replies.pop(0) if self.replies else None
                if reply is not None:
                    asyncio.get_running_loop().call_later(0.01, judge.hear, reply)

            async def lana_done():
                return None

            judge = JudgeLine(clinic, say=say, turn_start=turn_start, lana_done=lana_done, silence_s=0.05, hold_s=0.05)
            self.judges.append(judge)
            receptionist = judge
        self.built[clinic.id] = type(receptionist).__name__
        return receptionist


async def _judge_run(factory: ScriptedJudge) -> tuple[Case, list]:
    events: list = []
    case = Case(preferences=_prefs())
    await Orchestrator(case, events.append, max_clinics=2, receptionists=factory).run()
    return case, events


async def test_the_judge_plays_parkway_and_sunrise_is_simulated(world: World) -> None:
    world.readings = [{"verdict": "no", "evidence": "can't go that low"}] * 4
    factory = ScriptedJudge("It's $4,800 and the records fee is $250.", "Sure.", *["Sorry, we can't go that low."] * 4)

    case, events = await _judge_run(factory)

    assert factory.built == {"parkway": "JudgeLine", "sunrise": "FakeSim"}
    assert list(world.sims) == ["sunrise"]  # only Sunrise is simulated
    assert factory.phases[:3] == ["price", "hold", "negotiation"]
    assert factory.judges[0].stage == "negotiation"
    parkway = _lines(events, _call_ids(events, "parkway")[0])
    assert [line.text for line in parkway if line.speaker == Speaker.CLINIC][:2] == [
        "It's $4,800 and the records fee is $250.", "Sure.",
    ]
    assert case.negotiation is not None and case.negotiation.concessions == []
    assert case.status == S.OFFERS_READY


async def test_a_silent_judge_is_still_put_on_hold(world: World) -> None:
    factory = ScriptedJudge("It's $4,800 and the records fee is $250.", None)

    case, events = await _judge_run(factory)

    parkway = _lines(events, _call_ids(events, "parkway")[0])
    hold = next(i for i, line in enumerate(parkway) if line.text == HOLD_REQUEST)
    assert parkway[hold + 1].speaker == Speaker.AGENT  # no clinic line: nobody answered the hold
    assert parkway[hold + 1].text.startswith(RESUME_LINE)
    assert S.HOLDING in _statuses(events)


async def test_a_judge_who_hangs_up_is_not_called_back_and_the_run_goes_on(world: World) -> None:
    factory = ScriptedJudge(None, None)

    case, events = await _judge_run(factory)

    assert len(_call_ids(events, "parkway")) == 1  # never called back
    parkway = _lines(events, _call_ids(events, "parkway")[0])
    assert parkway[-1].text == CALL_BACK_LATER
    assert S.HOLDING not in _statuses(events)
    assert _call_ids(events, "sunrise")  # the run continues
    assert case.status == S.OFFERS_READY
    assert next(q for q in case.quotes if q.clinic_id == "parkway").all_in_price is None  # the quote stays partial
