"""Commands the patient can say at any time: "just show me", "start over", "yes, book it".

Plain keyword matching first, so the common phrases are fast and predictable. A phrase must
match whole (after filler words like "Lana," or "please" are dropped), so a request such as
"show me clinics near Dallas" is never read as "show me". Only short text that matches
nothing and is not a request for care goes to MODEL_FAST, and only when the caller allows it.
"""

import logging
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from app import guard
from app.config import get_settings
from app.integrations.meta_llm import call_json

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "commands.md"

# Text with this many words or more is a request or an answer, never a command.
CLASSIFY_MAX_WORDS = 6
# Words that make short text a request for care ("braces near Dallas"), which is never classified.
_REQUEST_RE = re.compile(
    r"\d|\$|\b(braces|aligners?|invisalign|orthodont\w*|dentists?|clinics|near|under|budget|"
    r"monthly|hsa|fsa|x-?rays?|evenings?)\b",
    re.IGNORECASE,
)


class Command(StrEnum):
    WATCH = "watch"
    CLINIC = "clinic"
    START_OVER = "start_over"
    YES_BOOK = "yes_book"
    NOT_YET = "not_yet"
    SKIP = "skip"


PHRASES: dict[Command, tuple[str, ...]] = {
    Command.WATCH: (
        "show me", "watch", "let me watch", "watch it", "watch it work", "watch the demo",
        "show me a demo", "show me how it works", "show me the demo", "demo",
    ),
    Command.CLINIC: (
        "clinic", "be the clinic", "i'll be the clinic", "i will be the clinic",
        "let me be the clinic", "can i be the clinic", "clinic mode",
    ),
    Command.START_OVER: (
        "start over", "start again", "let's start over", "let's start again", "begin again",
        "restart", "reset",
    ),
    Command.YES_BOOK: ("yes", "yeah", "yep", "yes book it", "book it", "sure", "go ahead", "do it"),
    Command.NOT_YET: ("not yet", "no", "nope", "not now", "maybe later", "later"),
    Command.SKIP: ("skip", "skip it", "skip that", "skip this", "skip this one", "pass", "next question"),
}
_BY_PHRASE = {phrase: command for command, phrases in PHRASES.items() for phrase in phrases}

_LEADING = re.compile(
    r"^(?:hey lana|hi lana|lana|hey|ok|okay|um|uh|so|please|just|can you|could you|"
    r"i want to|i'd like to)\s+"
)
_TRAILING = re.compile(r"\s+(?:please|lana|thanks|thank you|then)$")


class _Classified(BaseModel):
    command: Command | None = None


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    while True:
        trimmed = _TRAILING.sub("", _LEADING.sub("", text))
        if trimmed == text:
            return text
        text = trimmed


def match_command(text: str) -> Command | None:
    """The command `text` says in so many words, or None. No AI."""
    return _BY_PHRASE.get(_normalize(text))


async def parse_command(text: str, *, classify: bool = True) -> Command | None:
    """The command in what the patient said, or None if it is a request or an answer.

    Keywords first. If none match, `classify` is on, and the text is under
    CLASSIFY_MAX_WORDS words with nothing in it that asks for care, MODEL_FAST picks from
    the same list. A failed classifier call means no command; only guard.LimitHit is raised.
    """
    command = match_command(text)
    if (
        command is not None
        or not classify
        or not text.strip()
        or len(text.split()) >= CLASSIFY_MAX_WORDS
        or _REQUEST_RE.search(text)
    ):
        return command
    settings = get_settings()
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(text=text.strip())
    try:
        result = await call_json(prompt, _Classified, settings.model_fast, reasoning=settings.call_reasoning)
    except guard.LimitHit:
        raise
    except Exception as err:
        log.warning("command classifier failed, treating %r as no command: %s: %s", text, type(err).__name__, err)
        return None
    log.info("command classifier: %r -> %s", text, result.command)
    return result.command
