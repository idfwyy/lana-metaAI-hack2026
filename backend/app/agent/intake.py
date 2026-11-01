"""Intake: Lana's short chat that turns what the patient says into Preferences.

The model only pulls out what the patient said. Python merges it into the draft, picks the
next question from a fixed list in prompts/intake.md, and decides when intake is done.
"""

import difflib
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import get_settings
from app.integrations.meta_llm import call_json
from app.schemas.enums import Treatment
from app.schemas.preferences import Preferences

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "intake.md"
PROMPT_SPLIT = "<!-- lines -->"

MAX_QUESTIONS = 6

# The main DFW cities and a ZIP for each. A city without a ZIP gets its ZIP here; a place
# that matches none of them gets DEFAULT_ZIP (Arlington).
CITIES = {
    "Dallas": "75201",
    "Fort Worth": "76102",
    "Arlington": "76010",
    "Plano": "75024",
    "Irving": "75039",
    "Grand Prairie": "75050",
    "Garland": "75040",
    "Mesquite": "75149",
    "Frisco": "75034",
    "McKinney": "75069",
    "Denton": "76201",
    "Carrollton": "75006",
    "Richardson": "75080",
    "Lewisville": "75067",
    "Mansfield": "76063",
    "Euless": "76039",
    "Bedford": "76021",
    "Hurst": "76053",
    "Grapevine": "76051",
    "Southlake": "76092",
    "Keller": "76248",
    "Cedar Hill": "75104",
    "Duncanville": "75116",
    "DeSoto": "75115",
}
CITY_ZIPS = {name.lower(): zip_code for name, zip_code in CITIES.items()}
DEFAULT_ZIP = "76010"

# Fuzzy city matching, for speech-to-text slips such as "LinkedIn" for "Arlington".
# A match needs close spelling, or a close sound with longer keys and some shared spelling,
# so short words ("color", "garden") never turn into a city.
SPELLING_CUTOFF = 0.8
SOUND_CUTOFF = 0.85
SOUND_MIN_SPELLING = 0.4
SOUND_MIN_KEY = 4
_SOUND_MAP = str.maketrans({"g": "k", "q": "k", "c": "k", "d": "t", "b": "p", "v": "f", "z": "s", "x": "s"})
DEFAULT_DISTANCE_MI = 15.0
# A skipped budget means no cap in practice; every demo clinic lists well under this.
DEFAULT_BUDGET = 10_000

# The questions in the order Lana asks them, and the draft fields each one fills.
TOPIC_FIELDS: dict[str, tuple[str, ...]] = {
    "treatment": ("treatment",),
    "location": ("zip_code",),
    "budget": ("budget_max",),
    "pay": ("prefers_monthly", "pay_in_full_ok", "uses_hsa_fsa"),
    "records": ("has_xrays", "has_plan"),
    "must_haves": ("must_haves",),
}

_SKIP_RE = re.compile(
    r"\b(skip|pass|i don'?t know|idk|no idea|no clue|not sure|no preference|"
    r"doesn'?t matter|don'?t care|whatever)\b"
)
_MEDICAL_RE = re.compile(
    r"\b(do i (really |even |actually )?need|should i (get|do)|is (it|my \w+) (bad|severe|serious)|"
    r"how bad (is|are)|are my teeth)\b"
)
_PAY_IN_FULL_RE = re.compile(
    r"\b(in full|all at once|lump sum|one payment|in one go|full amount|"
    r"pay (it |everything )?all (now|at once|up ?front))\b"
)
_PAY_IN_FULL_NOT_RE = re.compile(
    r"\b(can'?t|cannot|can not|won'?t|not able to|unable to|no way)\b[^.?!]{0,25}"
    r"\b(in full|all at once|lump sum|one payment|in one go|full amount)\b"
)

_UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: (i + 2) * 10
    for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())
}


class PreferencesDraft(BaseModel):
    """Preferences while intake is still going: None means the patient has not said it."""

    treatment: Treatment | None = None
    zip_code: str | None = None
    city: str | None = Field(default=None, description="The place as the patient said it")
    max_distance_mi: float | None = None
    budget_max: int | None = None
    pay_in_full_ok: bool | None = None
    uses_hsa_fsa: bool | None = None
    prefers_monthly: bool | None = None
    has_xrays: bool | None = None
    has_plan: bool | None = None
    must_haves: list[str] | None = None
    free_times: list[str] | None = None


class _Heard(BaseModel):
    """What the model returns for one patient reply. Python checks and merges it."""

    treatment: Treatment | None = None
    zip_code: str | None = None
    city: str | None = None
    max_distance_mi: float | None = None
    budget: int | str | None = Field(default=None, description="Total budget in the patient's words")
    prefers_monthly: bool | None = None
    pay_in_full_ok: bool | None = None
    uses_hsa_fsa: bool | None = None
    has_xrays: bool | None = None
    has_plan: bool | None = None
    must_haves: list[str] | None = None
    free_times: list[str] | None = None
    skip: bool = False
    medical_question: bool = False


class IntakeTurn(BaseModel):
    filled: list[str] = Field(default_factory=list, description="Draft fields this reply filled")
    next_question: str | None = None
    done: bool = False
    spoken: str = Field(description="What Lana says next")


def _prompts() -> tuple[str, dict[str, str]]:
    prompt, lines = PROMPT_PATH.read_text(encoding="utf-8").split(PROMPT_SPLIT, 1)
    fixed = dict(re.findall(r"^- (\w+): (.+)$", lines, flags=re.MULTILINE))
    return prompt.strip(), fixed


def _words_to_int(text: str) -> int | None:
    """'five thousand five hundred' -> 5500. Reads the first run of number words only."""
    total, current, seen = 0, 0, False
    for word in re.findall(r"[a-z]+", text):
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in ("thousand", "grand", "k"):
            total += (current or 1) * 1000
            current = 0
        elif word in ("a", "and") and (seen or word == "a"):
            continue
        else:
            if seen:
                break
            continue
        seen = True
    return total + current if seen else None


def parse_budget(value: int | str) -> int | None:
    """Whole dollars from what the patient said: '5k' = 5000, 'under five thousand' = 5000."""
    if isinstance(value, int):
        return value if value >= 0 else None
    text = value.lower().replace(",", "").replace("$", "")
    amounts = [
        float(num) * (1000 if unit else 1)
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(k|grand|thousand)?\b", text)
    ]
    if amounts:
        return int(round(max(amounts)))
    return _words_to_int(text)


def city_zip(city: str) -> str:
    """Default ZIP for a city name; 'Dallas, TX' works too. Unknown cities get DEFAULT_ZIP."""
    name = city.lower().split(",")[0].strip()
    name = re.sub(r"\s+(tx|texas)$", "", name)
    return CITY_ZIPS.get(name, DEFAULT_ZIP)


def _sound(text: str) -> str:
    """A rough sound key: similar consonants merged, vowels and h/w/y dropped, repeats collapsed."""
    letters = re.sub(r"[^a-z]", "", text.lower()).translate(_SOUND_MAP)
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiouyhw]", "", letters))


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_city(text: str) -> tuple[str, str, bool] | None:
    """The DFW city in `text` as (name, ZIP, fuzzy), or None.

    An exact name (any case, spaces optional: "grape vine") wins. Otherwise the closest city
    by spelling or by sound, if it clears the cutoffs; `fuzzy` is True then.
    """
    words = re.findall(r"[a-z]+", text.lower())
    words = [w for w in words if w not in ("tx", "texas")]
    grams = [" ".join(words[i : i + n]) for n in (2, 1) for i in range(len(words) - n + 1)]
    squashed = {name.lower().replace(" ", ""): name for name in CITIES}
    for gram in grams:
        name = squashed.get(gram.replace(" ", ""))
        if name is not None:
            return name, CITIES[name], False

    best: tuple[float, str] | None = None
    for gram in grams:
        gram_sound = _sound(gram)
        for name in CITIES:
            spelling = _ratio(gram, name.lower())
            city_sound = _sound(name)
            sound = _ratio(gram_sound, city_sound)
            close = spelling >= SPELLING_CUTOFF or (
                sound >= SOUND_CUTOFF
                and spelling >= SOUND_MIN_SPELLING
                and min(len(gram_sound), len(city_sound)) >= SOUND_MIN_KEY
            )
            score = max(spelling, sound)
            if close and (best is None or score > best[0]):
                best = (score, name)
    if best is None:
        return None
    return best[1], CITIES[best[1]], True


def _clean_zip(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b\d{5}\b", value)
    return match.group(0) if match else None


def _clean_list(items: list[str] | None) -> list[str] | None:
    if items is None:
        return None
    return [item.strip() for item in items if item.strip()]


def _says_pay_in_full(text: str) -> bool:
    return bool(_PAY_IN_FULL_RE.search(text)) and not _PAY_IN_FULL_NOT_RE.search(text)


class Intake:
    def __init__(self) -> None:
        self.draft = PreferencesDraft()
        self.asked = 0
        self.current: str | None = None  # the topic Lana asked last; None before the first question
        self.skipped: set[str] = set()
        # Replies that left a topic unanswered. The second one sets its default: never a third ask.
        self.misses: dict[str, int] = dict.fromkeys(TOPIC_FIELDS, 0)
        self.done = False
        self._fuzzy_city: str | None = None  # a city fuzzy-matched from this reply, for Lana to confirm
        self._prompt, self._lines = _prompts()

    def first_line(self) -> str:
        return self._lines["greeting"].format(agent_name=get_settings().agent_name)

    def is_answered(self, topic: str) -> bool:
        return topic in self.skipped or any(
            getattr(self.draft, name) is not None for name in TOPIC_FIELDS[topic]
        )

    def _next_topic(self) -> str | None:
        return next((t for t in TOPIC_FIELDS if not self.is_answered(t)), None)

    def _format_draft(self) -> str:
        said = self.draft.model_dump(exclude_none=True, mode="json")
        if not said:
            return "Nothing yet."
        return "\n".join(f"- {name}: {value}" for name, value in said.items())

    async def hear(self, text: str) -> IntakeTurn:
        """Take one patient reply, merge what it says, and decide what Lana says next."""
        if self.done:
            raise RuntimeError("intake is already done")
        text = text.strip()
        lowered = text.lower().replace("’", "'")
        settings = get_settings()
        question = (
            self._lines[self.current] if self.current else self.first_line()
        )
        prompt = self._prompt.format(
            agent_name=settings.agent_name,
            question=question,
            draft=self._format_draft(),
            text=text,
        )
        heard = await call_json(prompt, _Heard, settings.model_fast, reasoning=settings.call_reasoning)

        medical = heard.medical_question or bool(_MEDICAL_RE.search(lowered))
        self._fuzzy_city = None
        filled = self._merge(heard, lowered, medical)

        current = self.current
        default = ""
        if current is not None and not self.is_answered(current):
            if not medical and (heard.skip or _SKIP_RE.search(lowered)):
                self.skipped.add(current)
            else:
                self.misses[current] += 1
                if self.misses[current] >= 2:
                    # Asked twice with no usable answer: use the default, say so, move on.
                    self.skipped.add(current)
                    default = self._lines[f"default_{current}"] + " "
                    log.info("intake used the default for %s after two unusable answers", current)

        topic = self._next_topic()
        # A question repeated after the no-advice line is the same question, not a new one.
        repeat = medical and topic is not None and topic == current
        if topic is not None and not repeat:
            if self.asked >= MAX_QUESTIONS:
                topic = None
            else:
                self.asked += 1
        self.current = topic

        lead = self._lines["no_advice"] + " " if medical else ""
        if topic is None:
            self.done = True
            # The confirmation names the place, so a fuzzy city needs no separate check.
            return IntakeTurn(filled=filled, done=True, spoken=lead + default + self._confirmation())
        if self._fuzzy_city:
            lead += self._lines["fuzzy_city"].format(city=self._fuzzy_city) + " "
        next_question = self._lines[topic]
        return IntakeTurn(filled=filled, next_question=next_question, spoken=lead + default + next_question)

    def _merge(self, heard: _Heard, text: str, medical: bool) -> list[str]:
        """Write what the patient said into the draft. A later answer replaces an earlier one."""
        updates: dict[str, object] = {}
        # "Do I need braces?" is not a choice of braces.
        if heard.treatment is not None and not medical:
            updates["treatment"] = heard.treatment

        zip_code = _clean_zip(heard.zip_code)
        city = heard.city.strip() if heard.city and heard.city.strip() else None
        known = match_city(city) if city is not None else None
        if known is None and city is None and zip_code is None and self.current == "location":
            # The model found no place, but the reply may hold a misheard city ("LinkedIn").
            known = match_city(text)
        if known is not None:
            city = known[0]
            if known[2]:
                self._fuzzy_city = city
                log.info("intake fuzzy-matched %r to %s", heard.city or text, city)
        if zip_code is not None:
            updates["zip_code"] = zip_code
            if city is None:
                self.draft.city = None
        elif known is not None:
            updates["zip_code"] = known[1]
        elif city is not None:
            updates["zip_code"] = city_zip(city)
        if city is not None:
            updates["city"] = city

        if heard.max_distance_mi is not None and heard.max_distance_mi > 0:
            updates["max_distance_mi"] = heard.max_distance_mi
        if heard.budget is not None:
            budget = parse_budget(heard.budget)
            if budget is not None:
                updates["budget_max"] = budget

        for name in ("prefers_monthly", "uses_hsa_fsa", "has_xrays", "has_plan"):
            value = getattr(heard, name)
            if value is not None:
                updates[name] = value
        if heard.pay_in_full_ok is True and not _says_pay_in_full(text):
            log.info("intake dropped pay_in_full_ok=true: patient did not clearly say it")
        elif heard.pay_in_full_ok is not None:
            updates["pay_in_full_ok"] = heard.pay_in_full_ok

        for name in ("must_haves", "free_times"):
            items = _clean_list(getattr(heard, name))
            if items is not None:
                updates[name] = items

        changed = [name for name, value in updates.items() if getattr(self.draft, name) != value]
        for name in changed:
            setattr(self.draft, name, updates[name])
        return changed

    def _confirmation(self) -> str:
        prefs = self.to_preferences()
        treatment = {
            Treatment.BRACES: "Braces",
            Treatment.ALIGNERS: "Aligners",
            Treatment.NOT_SURE: "Braces or aligners",
        }[prefs.treatment]
        place = self.draft.city or self.draft.zip_code
        parts = [f"{treatment} near {place}" if place else treatment]
        if self.draft.budget_max is not None:
            parts.append(f"under ${self.draft.budget_max:,}")
        parts.extend(prefs.must_haves)
        return self._lines["confirm"].format(summary=", ".join(parts))

    def to_preferences(self) -> Preferences:
        """The draft as Preferences, with the conservative default for anything not said."""
        d = self.draft
        return Preferences(
            treatment=d.treatment or Treatment.NOT_SURE,
            zip_code=d.zip_code or DEFAULT_ZIP,
            max_distance_mi=d.max_distance_mi or DEFAULT_DISTANCE_MI,
            budget_max=d.budget_max if d.budget_max is not None else DEFAULT_BUDGET,
            pay_in_full_ok=bool(d.pay_in_full_ok),
            uses_hsa_fsa=bool(d.uses_hsa_fsa),
            prefers_monthly=bool(d.prefers_monthly),
            has_xrays=bool(d.has_xrays),
            has_plan=bool(d.has_plan),
            must_haves=d.must_haves or [],
            free_times=d.free_times or [],
        )
