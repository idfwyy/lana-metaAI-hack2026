"""commands.py with call_json mocked. No network."""

import pytest

from app import guard
from app.agent import commands
from app.agent.commands import Command, match_command, parse_command


class FakeClassifier:
    """Stands in for call_json: records each call and returns the scripted command."""

    def __init__(self, command: str | None = None, error: Exception | None = None) -> None:
        self.command = command
        self.error = error
        self.prompts: list[str] = []

    async def __call__(self, prompt, schema, model, images=None, reasoning=None):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return schema.model_validate({"command": self.command})


@pytest.fixture
def classifier(monkeypatch):
    def install(command: str | None = None, error: Exception | None = None) -> FakeClassifier:
        fake = FakeClassifier(command, error)
        monkeypatch.setattr(commands, "call_json", fake)
        return fake

    return install


@pytest.mark.parametrize(
    ("said", "command"),
    [
        ("Just show me.", Command.WATCH),
        ("show me", Command.WATCH),
        ("Watch", Command.WATCH),
        ("Lana, just show me", Command.WATCH),
        ("Can you show me how it works?", Command.WATCH),
        ("I'll be the clinic", Command.CLINIC),
        ("I’ll be the clinic.", Command.CLINIC),
        ("Let me be the clinic", Command.CLINIC),
        ("clinic", Command.CLINIC),
        ("Start over", Command.START_OVER),
        ("Let's start over, please", Command.START_OVER),
        ("restart", Command.START_OVER),
        ("Yes", Command.YES_BOOK),
        ("Yes, book it!", Command.YES_BOOK),
        ("Go ahead", Command.YES_BOOK),
        ("Not yet", Command.NOT_YET),
        ("No thanks", Command.NOT_YET),
        ("Maybe later", Command.NOT_YET),
        ("Skip", Command.SKIP),
        ("Pass.", Command.SKIP),
        ("Next question", Command.SKIP),
    ],
)
async def test_phrases_map_to_commands(classifier, said, command) -> None:
    fake = classifier()  # answers None for anything it is asked
    assert await parse_command(said) == command
    if command is not None:
        assert fake.prompts == []  # a keyword matched; the LLM was not called


@pytest.mark.parametrize(
    "said",
    [
        "show me clinics near Dallas",
        "Find me braces near Dallas under $5,000",
        "I want to watch my budget, braces under 5k",
        "Yes I already have x-rays from my dentist",
        "no, I don't have a treatment plan yet",
    ],
)
async def test_patient_requests_are_not_commands(classifier, said) -> None:
    fake = classifier("watch")  # would say watch if it were asked
    assert await parse_command(said) is None
    assert fake.prompts == []  # requests for care, and six words or more, never reach the classifier


async def test_short_unmatched_text_goes_to_the_classifier(classifier) -> None:
    fake = classifier("watch")
    assert await parse_command("let me see it work") == Command.WATCH
    assert len(fake.prompts) == 1 and "let me see it work" in fake.prompts[0]

    fake = classifier(None)
    assert await parse_command("hmm, what now") is None
    assert len(fake.prompts) == 1


@pytest.mark.parametrize("said", ["braces please", "Dallas 75201", "under five grand", "monthly"])
async def test_short_requests_skip_the_classifier(classifier, said) -> None:
    fake = classifier("watch")
    assert await parse_command(said) is None
    assert fake.prompts == []


async def test_classify_off_skips_the_classifier(classifier) -> None:
    fake = classifier("watch")
    assert await parse_command("Dallas", classify=False) is None
    assert await parse_command("just show me", classify=False) == Command.WATCH  # keywords still work
    assert fake.prompts == []


async def test_a_classifier_failure_means_no_command(classifier) -> None:
    classifier(error=RuntimeError("Meta is down"))
    assert await parse_command("let me see it") is None


async def test_the_usage_limit_still_stops_the_case(classifier) -> None:
    classifier(error=guard.LimitHit("llm limit reached"))
    with pytest.raises(guard.LimitHit):
        await parse_command("let me see it")


@pytest.mark.parametrize(
    ("said", "command"),
    [
        ("Hold off.", Command.NOT_YET),
        ("Hold off for now, Lana", Command.NOT_YET),
        ("Not now", Command.NOT_YET),
        ("Maybe later.", Command.NOT_YET),
        ("Don't book it", Command.NOT_YET),
        ("No thanks.", Command.NOT_YET),
        ("Book it anyway", Command.YES_BOOK),
        ("Go ahead.", Command.YES_BOOK),
        ("Yes, book it", Command.YES_BOOK),
    ],
)
def test_callback_answers_including_the_over_budget_question(said, command) -> None:
    # "Want me to book it anyway, or hold off?" is answered without the classifier.
    assert match_command(said) == command
