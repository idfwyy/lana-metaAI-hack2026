"""Negotiator with the LLM mocked. No network."""

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from app.agent import negotiator
from app.agent.negotiator import (
    CLOSING_LINE,
    SOURCE_LINE,
    WAIT_LINE,
    Move,
    MoveKind,
    ReplyReading,
    Verdict,
    negotiate,
    phrase_move,
    pick_target,
    plan_moves,
    record_reply,
)
from app.config import get_settings
from app.data.loader import get_public_clinics
from app.schemas.enums import Speaker, Treatment
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "three_quotes.json"
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=get_settings().tz)


def _prefs(**overrides) -> Preferences:
    data = dict(
        treatment=Treatment.BRACES,
        zip_code="76010",
        max_distance_mi=15,
        budget_max=5000,
        pay_in_full_ok=True,
        prefers_monthly=True,
        must_haves=["evening hours"],
    )
    return Preferences(**{**data, **overrides})


def _quotes() -> dict[str, Quote]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {q["clinic_id"]: Quote.model_validate(q) for q in raw}


def _moves(prefs: Preferences | None = None) -> list[Move]:
    quotes = _quotes()
    return plan_moves(quotes["parkway"], list(quotes.values()), get_public_clinics(), prefs or _prefs())


MATCH = Move(
    kind=MoveKind.MATCH,
    clinic_name="Parkway Orthodontics",
    current_all_in=5050,
    cited_clinic="Sunrise Orthodontic Studio",
    cited_amount=4200,
)


class FakeChat:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def __call__(self, messages, model, reasoning=None) -> AsyncIterator[str]:
        self.calls += 1
        yield self.replies.pop(0)


def _reading(verdict: Verdict, **kw) -> dict:
    return {"verdict": verdict.value, **kw}


class FakeJson:
    def __init__(self, *readings: dict) -> None:
        self.readings = list(readings)

    async def __call__(self, prompt, schema, model, images=None, reasoning=None):
        return schema.model_validate(self.readings.pop(0))


class FakeReceptionist:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.heard: list[str] = []

    async def next_line(self, lana_line: str) -> str:
        self.heard.append(lana_line)
        return self.replies.pop(0)


# ---------- target ----------


def test_target_is_parkway_with_fixture() -> None:
    quotes = list(_quotes().values())
    assert pick_target(quotes, get_public_clinics(), _prefs(), now=NOW) == "parkway"


def test_no_target_when_best_fit_is_already_cheapest() -> None:
    quotes = _quotes()
    parkway = quotes["parkway"].model_copy(update={"all_in_price": 4100})

    target = pick_target([parkway, quotes["sunrise"], quotes["lakeside"]], get_public_clinics(), _prefs(), now=NOW)

    assert target is None


# ---------- moves ----------


def test_moves_in_order_citing_real_quote() -> None:
    moves = _moves()

    assert [m.kind for m in moves] == [
        MoveKind.MATCH,
        MoveKind.WAIVE_RECORDS,
        MoveKind.PAY_IN_FULL,
        MoveKind.RETAINERS,
    ]
    assert (moves[0].cited_clinic, moves[0].cited_amount) == ("Sunrise Orthodontic Studio", 4200)
    assert moves[1].records_fee == 250
    assert all(m.current_all_in == 5050 for m in moves)


def test_never_cites_a_clinic_not_in_the_quotes() -> None:
    quotes = _quotes()
    clinics = get_public_clinics()

    moves = plan_moves(quotes["parkway"], [quotes["parkway"], quotes["lakeside"]], clinics, _prefs())

    assert moves[0].cited_clinic == "Lakeside Family Orthodontics"
    assert moves[0].cited_amount == 4650
    cited = {m.cited_clinic for m in moves if m.cited_clinic}
    assert cited == {"Lakeside Family Orthodontics"}

    # A quote from a clinic we have no record of is never cited.
    ghost = quotes["sunrise"].model_copy(update={"clinic_id": "ghost"})
    moves = plan_moves(quotes["parkway"], [quotes["parkway"], ghost], clinics, _prefs())
    assert all(m.cited_clinic is None for m in moves)


def test_no_pay_in_full_move_when_not_ok() -> None:
    kinds = [m.kind for m in _moves(_prefs(pay_in_full_ok=False))]
    assert MoveKind.PAY_IN_FULL not in kinds
    assert kinds == [MoveKind.MATCH, MoveKind.WAIVE_RECORDS, MoveKind.RETAINERS]


def test_no_match_move_when_target_is_cheapest() -> None:
    quotes = _quotes()
    moves = plan_moves(quotes["sunrise"], list(quotes.values()), get_public_clinics(), _prefs())

    kinds = [m.kind for m in moves]
    assert MoveKind.MATCH not in kinds
    assert MoveKind.WAIVE_RECORDS not in kinds  # sunrise has no records fee
    assert kinds == [MoveKind.PAY_IN_FULL, MoveKind.RETAINERS]


# ---------- phrasing ----------


async def test_clean_phrasing_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    line = "Sunrise Orthodontic Studio quoted $4,200 all in, would you be able to match that?"
    monkeypatch.setattr(negotiator, "stream_chat", FakeChat(line))

    assert await phrase_move(MATCH) == line


@pytest.mark.parametrize(
    "bad",
    [
        "Sunrise Orthodontic Studio quoted $3,900 all in, could you match it?",  # made-up number
        "Lakeside Family Orthodontics quoted $4,200, could you match that?",  # wrong real clinic
        "Bright Smiles Dental quoted $4,200, could you match that?",  # invented clinic
    ],
)
async def test_made_up_number_or_clinic_falls_back_to_template(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    chat = FakeChat(bad, bad)
    monkeypatch.setattr(negotiator, "stream_chat", chat)

    line = await phrase_move(MATCH)

    assert chat.calls == 2  # regenerated once
    assert line == MATCH.template()
    assert "Sunrise Orthodontic Studio" in line and "$4,200" in line


async def test_bad_then_good_phrasing_uses_regenerated(monkeypatch: pytest.MonkeyPatch) -> None:
    good = "Sunrise Orthodontic Studio quoted $4,200 all in, could you match that?"
    monkeypatch.setattr(negotiator, "stream_chat", FakeChat("We were quoted $3,000 elsewhere.", good))

    assert await phrase_move(MATCH) == good


# ---------- replies ----------


async def test_unclear_reply_counts_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(negotiator, "call_json", FakeJson(_reading(Verdict.UNCLEAR)))

    assert await record_reply(MATCH, "Hmm, that's an interesting question.", 3.0, "c1") is None


async def test_yes_without_matching_evidence_counts_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        negotiator,
        "call_json",
        FakeJson(_reading(Verdict.YES, new_all_in=4200, evidence="we can match $4,200")),
    )

    assert await record_reply(MATCH, "Let me see what I can do.", 3.0, "c1") is None


async def test_yes_match_records_concession(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        negotiator,
        "call_json",
        FakeJson(_reading(Verdict.YES, new_all_in=4200, evidence="WE CAN DO 4200 all in")),
    )

    concession = await record_reply(MATCH, "Okay, we can do $4,200 all in.", 12.5, "c1")

    assert concession.amount == 850
    assert concession.evidence.evidence == "we can do $4,200 all in."
    assert concession.evidence.t_seconds == 12.5
    assert concession.evidence.call_id == "c1"


PAY_IN_FULL = MATCH.model_copy(
    update={"kind": MoveKind.PAY_IN_FULL, "cited_clinic": None, "cited_amount": None}
)


async def test_pay_in_full_amount_must_be_said(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        negotiator, "call_json", FakeJson(_reading(Verdict.YES, amount_off=240, evidence="we can do a discount"))
    )

    assert await record_reply(PAY_IN_FULL, "Yes, we can do a discount for that.", 1.0, "c1") is None


@pytest.mark.parametrize("current, expected", [(4200, 210), (5050, 253)])  # 252.5 rounds up
async def test_pay_in_full_percent_is_computed_in_python(
    monkeypatch: pytest.MonkeyPatch, current: int, expected: int
) -> None:
    move = PAY_IN_FULL.model_copy(update={"current_all_in": current})
    # The model's own amount_off is not in the line, so it is ignored.
    monkeypatch.setattr(
        negotiator,
        "call_json",
        FakeJson(_reading(Verdict.YES, amount_off=999, evidence="WE CAN DO 5 OFF")),
    )

    concession = await record_reply(move, "Sure, we can do 5% off if you pay in full.", 4.0, "c1")

    assert concession.amount == expected
    assert concession.evidence.value == expected
    assert concession.evidence.evidence == "we can do 5% off"
    assert concession.label == "Pay-in-full discount"


# ---------- the whole negotiation ----------


async def test_total_saved_and_stacking(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phrase(move):
        return move.template()

    monkeypatch.setattr(negotiator, "phrase_move", phrase)
    monkeypatch.setattr(
        negotiator,
        "call_json",
        FakeJson(
            _reading(Verdict.ASKS_WHICH_CLINIC),
            _reading(Verdict.PENDING),
            _reading(Verdict.YES, new_all_in=4200, evidence="we can match $4,200"),
            _reading(Verdict.YES, amount_off=100, evidence="$100 off"),
            _reading(Verdict.NO, evidence="no extra retainers"),
        ),
    )
    receptionist = FakeReceptionist(
        "Which clinic quoted that?",
        "Let me check with my manager.",
        "Good news, we can match $4,200.",
        "Sure, $100 off if paid in full.",
        "Sorry, no extra retainers.",
    )
    lines: list[tuple[Speaker, str, float]] = []

    result = await negotiate(
        _quotes()["parkway"], _moves(), receptionist, lambda s, t, ts: lines.append((s, t, ts)), "c1"
    )

    assert result.first_all_in == 5050
    assert result.final_all_in == 4100
    assert result.total_saved == 950 == sum(c.amount for c in result.concessions)
    assert [c.label for c in result.concessions] == ["Price match", "Pay-in-full discount"]
    assert result.refused == ["Second set of retainers"]
    # The records move was skipped after the all-in match, so it was never asked.
    assert not any("records fee" in h for h in receptionist.heard)
    assert receptionist.heard[1] == SOURCE_LINE.format(cited_clinic="Sunrise Orthodontic Studio", cited_amount=4200)
    assert receptionist.heard[2] == WAIT_LINE
    assert lines[0][1].startswith(
        f"Hi, this is {get_settings().agent_name}, an AI assistant calling for a patient, "
        "and this call is recorded. "
    )
    assert lines[-1][1] == CLOSING_LINE


async def test_at_most_four_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phrase(move):
        return move.template()

    moves = _moves() + _moves()  # 8 planned asks
    monkeypatch.setattr(negotiator, "phrase_move", phrase)
    monkeypatch.setattr(negotiator, "call_json", FakeJson(*[_reading(Verdict.NO)] * 8))
    receptionist = FakeReceptionist(*["No, sorry."] * 8)

    result = await negotiate(_quotes()["parkway"], moves, receptionist, lambda *a: None, "c1")

    assert len(receptionist.heard) == 4
    assert result.total_saved == 0
    assert len(result.refused) == 4


async def test_second_pending_counts_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phrase(move):
        return move.template()

    monkeypatch.setattr(negotiator, "phrase_move", phrase)
    monkeypatch.setattr(
        negotiator, "call_json", FakeJson(_reading(Verdict.PENDING), _reading(Verdict.PENDING))
    )
    receptionist = FakeReceptionist("Let me check.", "Still checking.")

    result = await negotiate(_quotes()["parkway"], _moves()[:1], receptionist, lambda *a: None, "c1")

    assert result.refused == ["Price match"]
    assert result.final_all_in == 5050


def test_reading_schema_defaults() -> None:
    assert ReplyReading(verdict=Verdict.NO).evidence == ""


async def test_start_t_continues_the_call_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phrase(move):
        return move.template()

    monkeypatch.setattr(negotiator, "phrase_move", phrase)
    monkeypatch.setattr(
        negotiator,
        "call_json",
        FakeJson(_reading(Verdict.YES, new_all_in=4200, evidence="we can match $4,200")),
    )
    receptionist = FakeReceptionist("Good news, we can match $4,200.")
    lines: list[tuple[Speaker, str, float]] = []

    result = await negotiate(
        _quotes()["parkway"],
        _moves()[:1],
        receptionist,
        lambda s, t, ts: lines.append((s, t, ts)),
        "c1",
        opening=False,
        start_t=42.0,
    )

    assert lines[0][2] == 42.0
    clinic_t = lines[1][2]
    assert clinic_t > 42.0
    assert result.concessions[0].evidence.t_seconds == clinic_t


async def test_opening_line_prepended_once_without_disclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def phrase(move):
        return move.template()

    monkeypatch.setattr(negotiator, "phrase_move", phrase)
    monkeypatch.setattr(negotiator, "call_json", FakeJson(*[_reading(Verdict.NO)] * 4))
    receptionist = FakeReceptionist(*["No, sorry."] * 4)
    lines: list[tuple[Speaker, str, float]] = []

    await negotiate(
        _quotes()["parkway"],
        _moves(),
        receptionist,
        lambda s, t, ts: lines.append((s, t, ts)),
        "c1",
        opening=False,
        opening_line="Thanks for holding.",
    )

    lana = [text for speaker, text, _ in lines if speaker == Speaker.AGENT]
    assert lana[0] == f"Thanks for holding. {_moves()[0].template()}"
    assert receptionist.heard[0] == lana[0]
    assert sum("Thanks for holding." in line for line in lana) == 1
    assert not any("AI assistant" in line for line in lana)
