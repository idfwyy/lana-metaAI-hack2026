"""Intake with call_json mocked. No network."""

import pytest

from app.agent import intake as intake_module
from app.agent.intake import (
    DEFAULT_BUDGET,
    MAX_QUESTIONS,
    Intake,
    city_zip,
    parse_budget,
)
from app.config import get_settings
from app.schemas.enums import Treatment


class FakeJson:
    """Stands in for call_json: returns one scripted reply per call, validated by the schema."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def __call__(self, prompt, schema, model, images=None, reasoning=None):
        self.prompts.append(prompt)
        return schema.model_validate(self.replies.pop(0) if self.replies else {})


@pytest.fixture
def fake_json(monkeypatch: pytest.MonkeyPatch):
    def install(*replies: dict) -> FakeJson:
        fake = FakeJson(list(replies))
        monkeypatch.setattr(intake_module, "call_json", fake)
        return fake

    return install


def _question(intake: Intake, topic: str) -> str:
    return intake._lines[topic]


def test_first_line_uses_agent_name() -> None:
    name = get_settings().agent_name
    assert Intake().first_line() == (
        f"Hey, I'm {name}, your personal health companion! What do you need help with?"
    )


async def test_three_fields_in_one_sentence_skip_those_questions(fake_json) -> None:
    fake_json({"treatment": "braces", "zip_code": "76010", "budget": "5000"})
    intake = Intake()

    turn = await intake.hear("I want braces near 76010, under 5000")

    assert set(turn.filled) == {"treatment", "zip_code", "budget_max"}
    assert intake.draft.treatment == Treatment.BRACES
    assert intake.draft.zip_code == "76010"
    assert intake.draft.budget_max == 5000
    assert turn.next_question == _question(intake, "pay")
    assert turn.spoken == turn.next_question
    assert not turn.done


@pytest.mark.parametrize(
    ("said", "dollars"),
    [
        ("5k", 5000),
        ("under 5k", 5000),
        ("4.5k", 4500),
        ("$4,800", 4800),
        ("under five thousand", 5000),
        ("five thousand five hundred dollars", 5500),
        (6000, 6000),
    ],
)
def test_parse_budget(said, dollars) -> None:
    assert parse_budget(said) == dollars


async def test_5k_becomes_5000(fake_json) -> None:
    fake_json({"budget": "5k"})
    intake = Intake()

    turn = await intake.hear("about 5k")

    assert turn.filled == ["budget_max"]
    assert intake.draft.budget_max == 5000


async def test_city_maps_to_its_zip(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas"})
    intake = Intake()

    turn = await intake.hear("braces near Dallas")

    assert intake.draft.city == "Dallas"
    assert intake.draft.zip_code == "75201"
    assert "zip_code" in turn.filled
    assert turn.next_question == _question(intake, "budget")


def test_city_zip_table() -> None:
    assert city_zip("Fort Worth") == "76102"
    assert city_zip("plano, TX") == "75024"
    assert city_zip("Irving Texas") == "75039"
    assert city_zip("Mansfield") == "76010"


async def test_unknown_city_keeps_text_and_default_zip(fake_json) -> None:
    fake_json({"city": "Mansfield"})
    intake = Intake()

    await intake.hear("I'm in Mansfield")

    assert intake.draft.city == "Mansfield"
    assert intake.to_preferences().zip_code == "76010"
    assert intake.to_preferences().max_distance_mi == 15


async def test_skip_moves_on_with_a_default(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas"}, {})
    intake = Intake()
    await intake.hear("braces near Dallas")
    assert intake.current == "budget"

    turn = await intake.hear("skip")

    assert intake.is_answered("budget")
    assert turn.next_question == _question(intake, "pay")
    assert intake.to_preferences().budget_max == DEFAULT_BUDGET


async def test_i_dont_know_moves_on_when_the_model_flags_skip(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas", "budget": "5k"}, {"skip": True})
    intake = Intake()
    await intake.hear("braces near Dallas under 5k")

    turn = await intake.hear("hmm, I'd rather not say")

    assert turn.next_question == _question(intake, "records")
    prefs = intake.to_preferences()
    assert (prefs.prefers_monthly, prefs.pay_in_full_ok, prefs.uses_hsa_fsa) == (False, False, False)


async def test_later_answer_corrects_earlier_one(fake_json) -> None:
    fake_json(
        {"treatment": "braces", "city": "Dallas"},
        {"treatment": "aligners", "zip_code": "76102"},
    )
    intake = Intake()
    await intake.hear("braces in Dallas")

    turn = await intake.hear("actually make that aligners, and my ZIP is 76102")

    assert set(turn.filled) == {"treatment", "zip_code"}
    assert intake.draft.treatment == Treatment.ALIGNERS
    assert intake.draft.zip_code == "76102"
    assert intake.draft.city is None


async def test_finishes_after_all_fields(fake_json) -> None:
    fake_json(
        {"treatment": "braces"},
        {"city": "Dallas"},
        {"budget": "under five thousand"},
        {"prefers_monthly": True},
        {"has_xrays": False, "has_plan": False},
        {"must_haves": ["evening hours"]},
    )
    intake = Intake()
    replies = ["braces", "Dallas", "under five thousand", "monthly", "no", "evenings"]

    turns = [await intake.hear(r) for r in replies]

    assert [t.done for t in turns] == [False] * 5 + [True]
    last = turns[-1]
    assert last.next_question is None
    assert last.spoken == "Got it. Braces near Dallas, under $5,000, evening hours. I'll start calling now."
    assert "—" not in last.spoken
    prefs = intake.to_preferences()
    assert prefs.treatment == Treatment.BRACES
    assert prefs.zip_code == "75201"
    assert prefs.budget_max == 5000
    assert prefs.prefers_monthly is True
    assert prefs.must_haves == ["evening hours"]
    with pytest.raises(RuntimeError):
        await intake.hear("one more thing")


async def test_everything_at_once_finishes_in_one_turn(fake_json) -> None:
    fake_json(
        {
            "treatment": "braces",
            "zip_code": "76010",
            "budget": "5000",
            "prefers_monthly": True,
            "pay_in_full_ok": True,
            "uses_hsa_fsa": True,
            "has_xrays": False,
            "must_haves": ["evening hours"],
        }
    )
    intake = Intake()

    turn = await intake.hear(
        "Braces near 76010 under 5000, monthly or pay in full, I have an HSA, no x-rays, evenings"
    )

    assert turn.done
    assert intake.asked == 0
    assert intake.to_preferences().pay_in_full_ok is True


async def test_never_asks_more_than_six_questions(fake_json) -> None:
    fake_json()  # the model finds nothing in any reply
    intake = Intake()
    questions = 0

    for _ in range(20):
        turn = await intake.hear("hmm")
        if turn.next_question:
            questions += 1
        if turn.done:
            break

    assert turn.done
    assert questions == MAX_QUESTIONS
    assert intake.asked == MAX_QUESTIONS
    prefs = intake.to_preferences()
    assert prefs.treatment == Treatment.NOT_SURE
    assert prefs.zip_code == "76010"


async def test_medical_question_gets_no_advice_and_same_question(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas"}, {"medical_question": True})
    intake = Intake()
    first = await intake.hear("braces near Dallas")
    asked = intake.asked

    turn = await intake.hear("Wait, are my teeth bad enough that I need braces?")

    no_advice = intake._lines["no_advice"]
    assert "can't advise on treatment" in no_advice
    assert "dentist or orthodontist" in no_advice
    assert turn.spoken == f"{no_advice} {first.next_question}"
    assert turn.next_question == first.next_question
    assert intake.asked == asked


async def test_do_i_need_braces_does_not_set_treatment(fake_json) -> None:
    # Even if the model sets treatment, a medical question is not a choice of braces.
    fake_json({"treatment": "braces"})
    intake = Intake()

    turn = await intake.hear("Do I need braces?")

    assert intake.draft.treatment is None
    assert turn.spoken.startswith(intake._lines["no_advice"])
    assert turn.next_question == _question(intake, "treatment")


async def test_pay_in_full_stays_false_unless_clearly_said(fake_json) -> None:
    fake_json({"prefers_monthly": True, "pay_in_full_ok": True})
    intake = Intake()

    turn = await intake.hear("monthly would be best I think")

    assert "pay_in_full_ok" not in turn.filled
    assert intake.draft.prefers_monthly is True
    assert intake.to_preferences().pay_in_full_ok is False


async def test_cant_pay_in_full_is_false(fake_json) -> None:
    fake_json({"pay_in_full_ok": True})
    intake = Intake()

    await intake.hear("I can't pay it in full")

    assert intake.to_preferences().pay_in_full_ok is False


async def test_pay_in_full_true_when_clearly_said(fake_json) -> None:
    fake_json({"pay_in_full_ok": True})
    intake = Intake()

    await intake.hear("I could pay in full if it's cheaper")

    assert intake.to_preferences().pay_in_full_ok is True


def test_no_em_dashes_in_lines() -> None:
    intake = Intake()
    for line in [intake.first_line(), *intake._lines.values()]:
        assert "—" not in line
