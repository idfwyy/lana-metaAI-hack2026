"""Extractor with call_json mocked. No network."""

from pathlib import Path

import pytest

from app.agent import extractor
from app.agent.extractor import Transcript, build_quote, find_evidence, says_all_in, update_checklist
from app.config import get_settings
from app.schemas.checklist import ClinicChecklist, SourcedAnswer
from app.schemas.enums import Speaker

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "sunrise_call.json"


def _sunrise() -> Transcript:
    return Transcript.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


class FakeJson:
    """Stands in for call_json: validates scripted data against the requested schema."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.prompts: list[str] = []

    async def __call__(self, prompt, schema, model, images=None, reasoning=None):
        self.prompts.append(prompt)
        return schema.model_validate(self.data)


@pytest.fixture
def fake_json(monkeypatch: pytest.MonkeyPatch):
    def install(data: dict) -> FakeJson:
        fake = FakeJson(data)
        monkeypatch.setattr(extractor, "call_json", fake)
        return fake

    return install


def _answer(value, evidence="said so", t=1.0) -> SourcedAnswer:
    return SourcedAnswer(
        value=value, speaker=Speaker.CLINIC, evidence=evidence, call_id="c1", t_seconds=t
    )


def test_fixture_starts_with_exact_disclosure() -> None:
    first = _sunrise().lines[0]
    assert first.speaker == Speaker.AGENT
    assert first.t_seconds == 0.0
    assert first.text.startswith(
        "Hi there! This is Lana, I'm an AI assistant calling for a patient. Just so you know, this call's recorded. "
    )


async def test_valid_evidence_gets_line_time_and_call_id(fake_json) -> None:
    fake_json({"total_price": {"value": 4200, "evidence": "$4,200 total"}})

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    answer = checklist.total_price
    assert answer is not None
    assert answer.value == 4200
    assert answer.evidence == "$4,200 total."
    assert answer.speaker == Speaker.CLINIC
    assert answer.call_id == "fixture-sunrise-1"
    assert answer.t_seconds == 9.6


async def test_evidence_not_in_transcript_is_dropped(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 3900, "evidence": "braces are $3,900 total"},
            "monthly_payment": {"value": 175, "evidence": "$175 a month"},
        }
    )

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    assert checklist.total_price is None
    assert checklist.monthly_payment is not None
    assert checklist.monthly_payment.t_seconds == 32.3


async def test_evidence_from_a_lana_line_is_dropped(fake_json) -> None:
    fake_json({"down_payment": {"value": 500, "evidence": "how much is the down payment"}})

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    assert checklist.down_payment is None


async def test_evidence_matches_ignoring_case_and_punctuation(fake_json) -> None:
    fake_json(
        {
            "records_fee": {"value": 0, "evidence": "NO THERES NO RECORDS   FEE"},
            "evening_hours": {"value": False, "evidence": "we close at 4 pm so no evening"},
        }
    )

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    assert checklist.records_fee is not None
    assert checklist.records_fee.evidence == "No, there's no records fee,"
    assert checklist.evening_hours is not None
    assert checklist.evening_hours.evidence == "we close at 4 PM, so no evening"
    assert checklist.evening_hours.t_seconds == 55.8


async def test_one_clinic_line_fills_two_fields(fake_json) -> None:
    transcript = Transcript.model_validate(
        {
            "call_id": "c2",
            "clinic_id": "parkway",
            "lines": [
                {"speaker": "agent", "text": "What's the monthly payment?", "t_seconds": 0.0},
                {"speaker": "clinic", "text": "It's $180 a month with $500 down.", "t_seconds": 1.5},
            ],
        }
    )
    fake = fake_json(
        {
            "monthly_payment": {"value": 180, "evidence": "$180 a month"},
            "down_payment": {"value": 500, "evidence": "$500 down"},
        }
    )

    checklist = await update_checklist(ClinicChecklist(), transcript)

    assert checklist.monthly_payment.value == 180
    assert checklist.monthly_payment.evidence == "$180 a month"
    assert checklist.down_payment.value == 500
    assert checklist.down_payment.evidence == "$500 down."
    assert checklist.monthly_payment.t_seconds == checklist.down_payment.t_seconds == 1.5
    assert "not only the one Lana just asked about" in fake.prompts[0]


def test_find_evidence_needs_whole_words() -> None:
    line = "Comprehensive braces are $4,200 total."
    assert find_evidence("$200 total", line) is None
    assert find_evidence("braces are 4200", line) == "braces are $4,200"
    assert find_evidence("", line) is None


async def test_unknown_fields_stay_none(fake_json) -> None:
    fake_json({"total_price": {"value": 4200, "evidence": "$4,200 total"}})

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    for name in ClinicChecklist.model_fields:
        if name not in ("total_price", "total_is_all_in"):
            assert getattr(checklist, name) is None, name


async def test_previous_answer_kept_when_model_returns_null(fake_json) -> None:
    fake = fake_json({"total_price": None})
    before = ClinicChecklist(total_price=_answer(4200, "$4,200 total.", 9.6))

    checklist = await update_checklist(before, _sunrise())

    assert checklist.total_price == before.total_price
    assert "total_price: 4200" in fake.prompts[0]


async def test_slots_get_the_clinic_time_zone(fake_json) -> None:
    fake_json(
        {
            "next_slots": {
                "value": ["2026-10-30T07:30:00", "2026-11-03T10:00:00"],
                "evidence": "Friday, October 30 at 7:30 AM or Tuesday, November 3 at 10:00 AM",
            }
        }
    )

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    slots = checklist.next_slots.value
    assert [s.tzinfo for s in slots] == [get_settings().tz] * 2
    assert slots[1].utcoffset().total_seconds() == -6 * 3600  # after the Nov 1 change


def test_all_in_adds_records_fee_when_not_included() -> None:
    checklist = ClinicChecklist(
        total_price=_answer(4800),
        records_fee=_answer(250),
        includes=_answer(["braces", "adjustments", "retainers"]),
    )

    quote = build_quote("parkway", checklist)

    assert quote.all_in_price == 5050
    assert quote.is_solid


def test_all_in_skips_records_fee_when_included() -> None:
    checklist = ClinicChecklist(
        total_price=_answer(4800),
        records_fee=_answer(250),
        includes=_answer(["Records", "retainers"]),
    )

    assert build_quote("parkway", checklist).all_in_price == 4800


def test_quote_missing_fields_and_unknown_price() -> None:
    quote = build_quote("sunrise", ClinicChecklist(records_fee=_answer(250)))

    assert quote.all_in_price is None
    assert not quote.is_solid
    assert quote.missing_fields == [
        "total_price",
        "includes",
        "monthly_payment",
        "down_payment",
        "next_slots",
        "evening_hours",
    ]


def test_quote_unknown_records_fee_adds_nothing() -> None:
    quote = build_quote("sunrise", ClinicChecklist(total_price=_answer(4200)))

    assert quote.all_in_price == 4200
    assert quote.is_solid
    assert "records_fee" in quote.missing_fields


# ---------- all-in totals: a fee is never counted twice ----------


def _call(*clinic_lines: str) -> Transcript:
    lines = [{"speaker": "agent", "text": "How much is the full treatment?", "t_seconds": 0.0}]
    lines += [{"speaker": "clinic", "text": t, "t_seconds": 2.0 + 4 * i} for i, t in enumerate(clinic_lines)]
    return Transcript.model_validate({"call_id": "c3", "clinic_id": "parkway", "lines": lines})


PARKWAY_LINE = "Our comprehensive treatment is $4,800 plus a $250 records fee, so $5,050 all together."


async def test_5050_all_together_is_not_charged_the_records_fee_again(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 5050, "evidence": "$5,050 all together", "total_is_all_in": True},
            "records_fee": {"value": 250, "evidence": "a $250 records fee"},
        }
    )

    checklist = await update_checklist(ClinicChecklist(), _call(PARKWAY_LINE))

    assert checklist.total_is_all_in is True
    assert checklist.records_fee.value == 250
    assert build_quote("parkway", checklist).all_in_price == 5050


async def test_treatment_plus_records_adds_the_fee(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 4800, "evidence": "$4,800 for treatment", "total_is_all_in": False},
            "records_fee": {"value": 250, "evidence": "plus $250 records"},
        }
    )

    checklist = await update_checklist(ClinicChecklist(), _call("It's $4,800 for treatment, plus $250 records."))

    assert checklist.total_is_all_in is False
    assert build_quote("parkway", checklist).all_in_price == 5050


async def test_total_including_records_is_the_price(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 4200, "evidence": "the total is $4,200 including records", "total_is_all_in": True},
            "records_fee": {"value": 150, "evidence": "records alone would be $150"},
        }
    )

    checklist = await update_checklist(
        ClinicChecklist(), _call("So the total is $4,200 including records. Records alone would be $150.")
    )

    assert build_quote("lakeside", checklist).all_in_price == 4200


async def test_all_in_words_override_a_model_that_says_false(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 5050, "evidence": "$5,050 all in", "total_is_all_in": False},
            "records_fee": {"value": 250, "evidence": "$250 records fee"},
        }
    )

    checklist = await update_checklist(
        ClinicChecklist(), _call("Treatment with the $250 records fee comes to $5,050 all in.")
    )

    assert checklist.total_is_all_in is True
    assert build_quote("parkway", checklist).all_in_price == 5050


def test_all_in_words_in_the_evidence_count_even_without_the_flag() -> None:
    checklist = ClinicChecklist(
        total_price=_answer(5050, "$5,050 all together."),
        records_fee=_answer(250),
    )

    assert checklist.total_is_all_in is False
    assert build_quote("parkway", checklist).all_in_price == 5050


async def test_a_later_treatment_price_never_replaces_the_all_in_total(fake_json) -> None:
    fake_json(
        {
            "total_price": {"value": 4800, "evidence": "the $4,800 covers adjustments", "total_is_all_in": False},
            "records_fee": {"value": 250, "evidence": "a $250 records fee"},
        }
    )
    before = ClinicChecklist(total_price=_answer(5050, "$5,050 all together.", 2.0), total_is_all_in=True)

    checklist = await update_checklist(before, _call(PARKWAY_LINE, "And the $4,800 covers adjustments."))

    assert checklist.total_price.value == 5050 and checklist.total_is_all_in
    assert build_quote("parkway", checklist).all_in_price == 5050


async def test_the_model_sees_the_all_in_flag(fake_json) -> None:
    fake = fake_json({})
    before = ClinicChecklist(total_price=_answer(5050, "$5,050 all together.", 2.0), total_is_all_in=True)

    await update_checklist(before, _call(PARKWAY_LINE))

    assert "total_price: 5050" in fake.prompts[0]
    assert "total_is_all_in: True" in fake.prompts[0]
    assert "total_is_all_in" in fake.prompts[0].split("## Rules")[0]  # the prompt explains it


@pytest.mark.parametrize(
    "evidence, all_in",
    [
        ("$5,050 all together", True),
        ("$4,800 plus a $250 records fee, so $5,050 all together", True),
        ("the total is $4,200 including records", True),
        ("$4,500 all-in", True),
        ("$4,200 total.", True),
        ("$3,990 out the door", True),
        ("everything included, $4,650", True),
        ("$4,800 for treatment", False),
        ("$4,800 total, plus records", False),
        ("$4,800, not including records", False),
        ("$4,800 for braces", False),
    ],
)
def test_says_all_in(evidence, all_in) -> None:
    assert says_all_in(evidence) is all_in
