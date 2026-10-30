"""Extractor with call_json mocked. No network."""

from pathlib import Path

import pytest

from app.agent import extractor
from app.agent.extractor import Transcript, build_quote, find_evidence, update_checklist
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
        "Hi, this is Lana, an AI assistant calling for a patient, and this call is recorded. "
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


def test_find_evidence_needs_whole_words() -> None:
    line = "Comprehensive braces are $4,200 total."
    assert find_evidence("$200 total", line) is None
    assert find_evidence("braces are 4200", line) == "braces are $4,200"
    assert find_evidence("", line) is None


async def test_unknown_fields_stay_none(fake_json) -> None:
    fake_json({"total_price": {"value": 4200, "evidence": "$4,200 total"}})

    checklist = await update_checklist(ClinicChecklist(), _sunrise())

    for name in ClinicChecklist.model_fields:
        if name != "total_price":
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
