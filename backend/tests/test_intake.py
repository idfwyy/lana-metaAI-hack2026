"""Intake with call_json mocked. No network."""

import pytest

from app.agent import intake as intake_module
from app.agent.intake import (
    CITIES,
    DEFAULT_BUDGET,
    INTAKE_REASONING,
    MAX_QUESTIONS,
    NEAR_ME_DISTANCE_MI,
    Intake,
    city_zip,
    fillers,
    match_city,
    parse_budget,
    reply_problem,
)
from app.config import get_settings
from app.schemas.enums import Treatment

TURN_KEYS = {"updates", "answered", "asking", "reply", "done", "medical_question"}


class FakeJson:
    """Stands in for call_json: one scripted turn per call, validated by the schema.

    A dict without turn keys is just what the patient said (updates), with no reply from the
    model, so Lana falls back to the fixed question.
    """

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.reasoning: list[str | None] = []

    async def __call__(self, prompt, schema, model, images=None, reasoning=None):
        self.prompts.append(prompt)
        self.reasoning.append(reasoning)
        reply = self.replies.pop(0) if self.replies else {}
        return schema.model_validate(reply if set(reply) & TURN_KEYS else {"updates": reply})


@pytest.fixture
def fake_json(monkeypatch: pytest.MonkeyPatch):
    def install(*replies: dict) -> FakeJson:
        fake = FakeJson(list(replies))
        monkeypatch.setattr(intake_module, "call_json", fake)
        return fake

    return install


def _question(intake: Intake, topic: str) -> str:
    return intake._lines[topic]


def _turn(reply: str, asking: str | None, answered=(), done=False, **updates) -> dict:
    return {"updates": updates, "answered": list(answered), "asking": asking, "reply": reply, "done": done}


ALL_BUT_MUST_HAVES = {
    "treatment": "braces", "city": "Dallas", "budget": "5000",
    "prefers_monthly": True, "has_xrays": False, "has_plan": False,
}


def test_first_line_uses_agent_name() -> None:
    name = get_settings().agent_name
    assert Intake().first_line() == (
        f"Hey, I'm {name}, your personal health companion! What do you need help with?"
    )


def test_fillers_rotate_from_the_prompt_file() -> None:
    assert fillers() == ["Got it.", "Mm-hm.", "Okay.", "Sure."]


async def test_the_model_reads_the_whole_conversation_at_low_reasoning(fake_json) -> None:
    fake = fake_json(
        _turn("Braces, got it. Which city should I search near?", "location", ["treatment"], treatment="braces"),
        {},
    )
    intake = Intake(greeting="Hey, I'm Lana! Heads-up, this is a demo. What do you need?")

    await intake.hear("I want braces")
    await intake.hear("hmm")

    assert fake.reasoning == [INTAKE_REASONING, INTAKE_REASONING] and INTAKE_REASONING == "low"
    second = fake.prompts[1]
    assert "Heads-up, this is a demo" in second  # the greeting she actually said
    assert "Patient: I want braces" in second
    assert "Braces, got it. Which city should I search near?" in second
    assert "Patient: hmm" in second


async def test_a_good_reply_is_said_as_is(fake_json) -> None:
    reply = "Braces near Dallas, nice. What's the most you'd want to spend?"
    fake_json(_turn(reply, "budget", ["treatment", "location"], treatment="braces", city="Dallas"))
    intake = Intake()

    turn = await intake.hear("braces near Dallas")

    assert turn.spoken == reply
    assert turn.next_question == reply
    assert intake.current == "budget"


async def test_three_fields_in_one_sentence(fake_json) -> None:
    reply = "Got it, braces under $5,000 near Arlington. How would you like to pay?"
    fake_json(_turn(reply, "pay", treatment="braces", zip_code="76010", budget="5000"))
    intake = Intake()

    turn = await intake.hear("I want braces near 76010, under 5000")

    assert set(turn.filled) == {"treatment", "zip_code", "budget_max"}
    assert intake.draft.treatment == Treatment.BRACES
    assert intake.draft.zip_code == "76010"
    assert intake.draft.budget_max == 5000
    assert turn.spoken == reply
    assert not turn.done


async def test_three_fields_with_no_reply_fall_back_to_the_next_fixed_question(fake_json) -> None:
    fake_json({"treatment": "braces", "zip_code": "76010", "budget": "5000"})
    intake = Intake()

    turn = await intake.hear("I want braces near 76010, under 5000")

    assert turn.next_question == _question(intake, "pay")
    assert turn.spoken == turn.next_question


async def test_no_nothing_else_answers_must_haves(fake_json) -> None:
    fake_json(ALL_BUT_MUST_HAVES, {})  # the model hears nothing in "No, nothing else."
    intake = Intake()
    first = await intake.hear("Braces near Dallas under 5k, monthly, no x-rays")
    assert first.next_question == _question(intake, "must_haves")

    turn = await intake.hear("No, nothing else.")

    assert turn.done
    assert intake.draft.must_haves == []
    assert intake.to_preferences().must_haves == []
    assert turn.spoken.startswith("Got it. Braces near Dallas, under $5,000.")


@pytest.mark.parametrize("said", ["Nope, that's it.", "no", "Nothing", "I'm good"])
async def test_other_ways_to_say_nothing(fake_json, said) -> None:
    fake_json(ALL_BUT_MUST_HAVES, {})
    intake = Intake()
    await intake.hear("Braces near Dallas under 5k, monthly, no x-rays")

    assert (await intake.hear(said)).done


async def test_near_me_answers_must_haves(fake_json) -> None:
    fake_json({"treatment": "braces", "budget": "5k"})
    intake = Intake()

    turn = await intake.hear("Braces near me, under 5k")

    assert intake.is_answered("must_haves")
    assert intake.draft.must_haves == ["close by"]
    assert intake.draft.max_distance_mi == NEAR_ME_DISTANCE_MI == 8
    assert {"must_haves", "max_distance_mi"} <= set(turn.filled)
    assert intake.current == "location"  # still needs a city, never asks for must-haves
    assert turn.next_question == _question(intake, "location")


async def test_near_me_keeps_a_distance_the_patient_said(fake_json) -> None:
    fake_json({"treatment": "braces", "max_distance_mi": 5})
    intake = Intake()

    await intake.hear("braces close by, within 5 miles")

    assert intake.draft.max_distance_mi == 5
    assert intake.draft.must_haves == ["close by"]


async def test_never_asks_about_a_field_already_answered(fake_json) -> None:
    fake_json(_turn("Great. Are you after braces or aligners?", "treatment", treatment="braces", city="Dallas"))
    intake = Intake()

    turn = await intake.hear("braces near Dallas")

    assert intake.current == "budget"
    assert turn.spoken == _question(intake, "budget")


async def test_important_topics_come_first(fake_json) -> None:
    fake_json(_turn("Nice. Do you already have x-rays?", "records", treatment="braces"))
    intake = Intake()

    turn = await intake.hear("braces please")

    assert intake.current == "location"
    assert turn.spoken == _question(intake, "location")


async def test_never_repeats_her_previous_line(fake_json) -> None:
    location = Intake()._lines["location"]
    fake_json({"treatment": "braces"}, _turn(location, "location"))
    intake = Intake()
    first = await intake.hear("braces")
    assert first.spoken == location

    second = await intake.hear("hmm, let me think")

    assert second.spoken != first.spoken
    assert second.spoken == _question(intake, "again_location")


async def test_two_unusable_answers_use_the_default_and_never_ask_a_third_time(fake_json) -> None:
    fake_json({"treatment": "braces"}, {}, {})
    intake = Intake()
    await intake.hear("braces")
    location = _question(intake, "location")
    assert location == "Which city or ZIP code should I search near?"

    second = await intake.hear("blorp")
    assert second.spoken == _question(intake, "again_location")  # asked once more, in other words

    third = await intake.hear("blorp blorp")
    assert location not in third.spoken
    assert third.spoken == f"{intake._lines['default_location']} {_question(intake, 'budget')}"
    assert intake.current == "budget"
    assert intake.to_preferences().zip_code == "76010"


@pytest.mark.parametrize(
    "reply",
    [
        "Sounds like you need braces. Where are you located?",
        "Aligners would be the best fit for you. Which city are you in?",
        "I'd recommend braces for that. What city?",
        "Braces — great choice. Which city?",
        "Braces, nice. That helps a lot. I can search lots of places. Which city?",
        "Braces, nice.",
        "Which city? And what's your budget?",
    ],
)
async def test_a_bad_reply_falls_back_to_the_fixed_question(fake_json, reply) -> None:
    fake_json(_turn(reply, "location", treatment="braces"))
    intake = Intake()

    turn = await intake.hear("I think I want braces")

    assert turn.spoken == _question(intake, "location")
    assert "—" not in turn.spoken


def test_reply_problem() -> None:
    assert reply_problem("Okay, braces. Which city?", None) is None
    assert reply_problem("Okay. Which city?", "Okay. Which city?") == "same as her previous line"
    assert reply_problem("You don't need treatment yet. Which city?", None) == "medical opinion"
    assert reply_problem("", None) == "empty"


async def test_skip_moves_on_with_a_default(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas"}, {})
    intake = Intake()
    await intake.hear("braces near Dallas")
    assert intake.current == "budget"

    turn = await intake.hear("skip")

    assert intake.is_answered("budget")
    assert turn.next_question == _question(intake, "pay")
    assert intake.to_preferences().budget_max == DEFAULT_BUDGET


async def test_doesnt_matter_counts_when_the_model_marks_it_answered(fake_json) -> None:
    fake_json(
        {"treatment": "braces", "city": "Dallas", "budget": "5k"},
        _turn("No problem. Do you have x-rays yet?", "records", ["pay"]),
    )
    intake = Intake()
    await intake.hear("braces near Dallas under 5k")

    turn = await intake.hear("hmm, I'd rather not say")

    assert turn.spoken == "No problem. Do you have x-rays yet?"
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
    assert last.spoken == "Got it. Braces near Dallas, under $5,000, evening hours. I'm calling clinics now. You can watch, or step away and I'll call you back."
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


async def test_the_model_can_end_once_the_important_topics_are_in(fake_json) -> None:
    fake_json(_turn("Sounds good.", None, ["treatment", "location", "budget"], done=True,
                    treatment="braces", city="Dallas", budget="5k"))
    intake = Intake()

    turn = await intake.hear("Braces near Dallas under 5k, that's all, just find me something")

    assert turn.done
    assert turn.spoken.startswith("Got it. Braces near Dallas, under $5,000.")
    prefs = intake.to_preferences()
    assert prefs.must_haves == [] and prefs.pay_in_full_ok is False


async def test_the_model_cannot_end_before_the_budget(fake_json) -> None:
    fake_json(_turn("Sounds good.", None, done=True, treatment="braces", city="Dallas"))
    intake = Intake()

    turn = await intake.hear("Braces near Dallas, go ahead")

    assert not turn.done
    assert turn.spoken == _question(intake, "budget")


async def test_finish_confirms_with_defaults(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "Dallas"}, {"budget": "5k"})
    intake = Intake()
    await intake.hear("braces near Dallas")

    turn = await intake.hear("about 5k", finish=True)

    assert turn.done
    assert turn.spoken.startswith("Got it. Braces near Dallas, under $5,000.")


async def test_never_more_than_six_turns(fake_json) -> None:
    fake_json()  # the model finds nothing in any reply
    intake = Intake()
    questions = 0
    spoken: list[str] = []

    for _ in range(20):
        turn = await intake.hear("hmm")
        spoken.append(turn.spoken)
        if turn.next_question:
            questions += 1
        if turn.done:
            break

    assert turn.done
    assert questions == MAX_QUESTIONS == 6
    assert intake.asked == MAX_QUESTIONS
    assert all(a != b for a, b in zip(spoken, spoken[1:]))  # never the same line twice in a row
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
    # Even if the model sets treatment and marks it answered, a medical question is not a choice.
    fake_json(_turn("Braces are great. Where are you?", "location", ["treatment"], treatment="braces"))
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


async def test_budget_is_parsed_in_python_whatever_the_model_says(fake_json) -> None:
    fake_json(_turn("Got it. Which city?", "location", ["budget"], treatment="braces", budget="under 4.5k"))
    intake = Intake()

    await intake.hear("braces, under four and a half grand")

    assert intake.draft.budget_max == 4500


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
    assert city_zip("Mansfield") == "76063"
    assert city_zip("Waco") == "76010"
    assert len(CITIES) == 24


async def test_unknown_city_keeps_text_and_default_zip(fake_json) -> None:
    fake_json({"city": "Waco"})
    intake = Intake()

    await intake.hear("I'm in Waco")

    assert intake.draft.city == "Waco"
    assert intake.to_preferences().zip_code == "76010"
    assert intake.to_preferences().max_distance_mi == 15


def test_no_em_dashes_in_lines() -> None:
    intake = Intake()
    for line in [intake.first_line(), *intake._lines.values()]:
        assert "—" not in line


def test_every_topic_has_a_rephrase_and_a_default() -> None:
    intake = Intake()
    for topic in intake_module.TOPIC_FIELDS:
        assert intake._lines[f"again_{topic}"] != intake._lines[topic]
        assert intake._lines[f"default_{topic}"]


@pytest.mark.parametrize(
    "said, expected",
    [
        ("LinkedIn", ("Arlington", "76010", True)),
        ("fort worth", ("Fort Worth", "76102", False)),
        ("near Plano, TX", ("Plano", "75024", False)),
        ("grape vine", ("Grapevine", "76051", False)),
        ("mckinny", ("McKinney", "75069", True)),
        ("xyzzy", None),
        ("blorptank", None),
        ("color", None),
        ("garden", None),
        ("near me", None),
    ],
)
def test_match_city(said, expected) -> None:
    assert match_city(said) == expected


async def test_misheard_city_is_matched_and_confirmed(fake_json) -> None:
    fake_json({"treatment": "braces"}, {})  # the model finds no place in "LinkedIn"
    intake = Intake()
    await intake.hear("braces")
    assert intake.current == "location"

    turn = await intake.hear("LinkedIn")

    assert intake.draft.city == "Arlington"
    assert intake.draft.zip_code == "76010"
    assert turn.spoken == f"Arlington, got it. {_question(intake, 'budget')}"


async def test_fuzzy_city_leads_the_models_question(fake_json) -> None:
    fake_json(_turn("Linked In, sure. What's your budget?", "budget", treatment="braces", city="Linked In"))
    intake = Intake()

    turn = await intake.hear("braces near Linked In")

    assert intake.draft.city == "Arlington"
    assert turn.spoken == "Arlington, got it. What's your budget?"


async def test_exact_city_needs_no_confirmation(fake_json) -> None:
    fake_json({"treatment": "braces", "city": "fort worth"})
    intake = Intake()

    turn = await intake.hear("braces in fort worth")

    assert intake.draft.city == "Fort Worth" and intake.draft.zip_code == "76102"
    assert turn.spoken == _question(intake, "budget")
