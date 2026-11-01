"""Intake: Lana's short chat that turns what the patient says into Preferences.

Each turn the model reads the whole conversation and the draft, and returns what the patient
said, which topics they addressed, and Lana's next line. Python still owns the rules: it
merges the values (budget, pay in full, cities, defaults), checks her line, falls back to a
fixed question from prompts/intake.md when the line fails a check, and decides when intake
is done.
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
INTAKE_REASONING = "low"
MAX_REPLY_SENTENCES = 2

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

# The topics in the order Lana asks them when she has no better idea, and the draft fields
# each one fills. The first three matter most and are asked before the others.
TOPIC_FIELDS: dict[str, tuple[str, ...]] = {
    "treatment": ("treatment",),
    "location": ("zip_code",),
    "budget": ("budget_max",),
    "pay": ("prefers_monthly", "pay_in_full_ok", "uses_hsa_fsa"),
    "records": ("has_xrays", "has_plan"),
    "must_haves": ("must_haves",),
}
IMPORTANT = ("treatment", "location", "budget")
TOPIC_HINTS = {
    "treatment": "braces, clear aligners, or not sure",
    "location": "the city or ZIP to search near",
    "budget": "the most they want to spend in total",
    "pay": "monthly, in full if cheaper, or with an HSA or FSA",
    "records": "whether they already have x-rays or a treatment plan",
    "must_haves": "anything a clinic must have, like evening hours",
}

# "Near me" answers must-haves: a short distance, and "close by" for the ranker and the calls.
NEAR_ME_DISTANCE_MI = 8.0
NEAR_ME_MUST = "close by"

_SKIP_RE = re.compile(
    r"\b(skip|pass|i don'?t know|idk|no idea|no clue|not sure|no preference|"
    r"doesn'?t matter|don'?t care|whatever)\b"
)
_NEAR_ME_RE = re.compile(r"\b(near me|close by|nearby|close to me|around here|near (my|our) (house|home|place))\b")
# "No, nothing else" after the must-haves question.
_NOTHING_RE = re.compile(
    r"\b(no+|nope|nah|nothing|none|not really|that'?s (it|all|everything)|i'?m good|we'?re good|all good)\b"
)
# Lana's own line giving an opinion on treatment. Such a line is never said.
_ADVICE_RE = re.compile(
    r"\b(you|you'?ll|you'?d)\b[^.?!]{0,25}\b(need|should (get|go with|choose|pick)|don'?t need|won'?t need)\b"
    r"[^.?!]{0,25}\b(braces|aligners|invisalign|treatment)\b"
    r"|\bi'?d? (recommend|suggest)\b"
    r"|\b(right|best|better) (choice |option |fit )?for you\b"
    r"|\b(severe|mild|serious) case\b"
    r"|\bsounds like you need\b"
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
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
    """The preferences the model found in one patient reply. Python checks and merges them."""

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


class _Turn(BaseModel):
    """What the model returns for one turn: what was said, and Lana's next line."""

    updates: _Heard = Field(default_factory=_Heard)
    answered: list[str] = Field(default_factory=list, description="Topics the patient addressed")
    asking: str | None = Field(default=None, description="The missing topic the reply asks about")
    reply: str = ""
    done: bool = False
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


def fillers() -> list[str]:
    """Lana's short fillers ("Got it.", "Mm-hm."), said while the model works on her reply."""
    return [f.strip() for f in _prompts()[1]["fillers"].split("|") if f.strip()]


def _norm(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


def reply_problem(reply: str, previous: str | None) -> str | None:
    """Why Lana can't say this model line, or None. The fixed question is said instead."""
    if not reply.strip():
        return "empty"
    if "—" in reply or "–" in reply:
        return "em dash"
    if _ADVICE_RE.search(reply.lower().replace("’", "'")):
        return "medical opinion"
    if len([s for s in _SENTENCE_RE.split(reply.strip()) if s.strip()]) > MAX_REPLY_SENTENCES:
        return "more than two sentences"
    if reply.count("?") != 1 or not reply.rstrip().endswith("?"):
        return "not one question"
    if previous is not None and _norm(reply) == _norm(previous):
        return "same as her previous line"
    return None


def _question_part(reply: str) -> str:
    """The question sentence of a checked reply, for when Python leads with its own line."""
    sentences = [s for s in _SENTENCE_RE.split(reply.strip()) if s.strip()]
    return next((s for s in reversed(sentences) if s.endswith("?")), reply.strip())


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
    def __init__(self, greeting: str | None = None) -> None:
        self.draft = PreferencesDraft()
        self.asked = 0
        self.current: str | None = None  # the topic Lana asked last; None before the first question
        self.answered: set[str] = set()  # topics addressed without a value: "skip", "nothing", defaults
        # Replies that left a topic unanswered. The second one sets its default: never a third ask.
        self.misses: dict[str, int] = dict.fromkeys(TOPIC_FIELDS, 0)
        self.done = False
        self._fuzzy_city: str | None = None  # a city fuzzy-matched from this reply, for Lana to confirm
        self._prompt, self._lines = _prompts()
        # The whole chat as (speaker, text), starting with the greeting Lana actually said.
        self.history: list[tuple[str, str]] = [("lana", greeting or self.first_line())]

    def first_line(self) -> str:
        return self._lines["greeting"].format(agent_name=get_settings().agent_name)

    def is_answered(self, topic: str) -> bool:
        return topic in self.answered or any(
            getattr(self.draft, name) is not None for name in TOPIC_FIELDS[topic]
        )

    def missing(self) -> list[str]:
        """Unanswered topics, the important ones first."""
        return [t for t in TOPIC_FIELDS if not self.is_answered(t)]

    def _previous(self) -> str | None:
        return next((text for speaker, text in reversed(self.history) if speaker == "lana"), None)

    def _format_draft(self) -> str:
        said = self.draft.model_dump(exclude_none=True, mode="json")
        if not said:
            return "Nothing yet."
        return "\n".join(f"- {name}: {value}" for name, value in said.items())

    def _format_history(self) -> str:
        name = get_settings().agent_name
        return "\n".join(f"{name if who == 'lana' else 'Patient'}: {text}" for who, text in self.history)

    def _fixed(self, topic: str, previous: str | None) -> str:
        """The fixed question for a topic, or its rephrase if that was Lana's previous line."""
        line = self._lines[topic]
        if previous is not None and _norm(line) == _norm(previous):
            line = self._lines[f"again_{topic}"]
        return line

    async def hear(self, text: str, finish: bool = False) -> IntakeTurn:
        """Take one patient reply, merge what it says, and decide what Lana says next.

        finish=True ends intake on this turn: anything still missing takes its default.
        """
        if self.done:
            raise RuntimeError("intake is already done")
        text = text.strip()
        lowered = text.lower().replace("’", "'")
        settings = get_settings()
        previous = self._previous()
        asked = self.current
        self.history.append(("patient", text))
        missing = self.missing()
        prompt = self._prompt.format(
            agent_name=settings.agent_name,
            conversation=self._format_history(),
            draft=self._format_draft(),
            answered=", ".join(t for t in TOPIC_FIELDS if t not in missing) or "None yet.",
            missing="\n".join(f"- {t}: {TOPIC_HINTS[t]}" for t in missing) or "Nothing.",
            previous=previous or "None.",
            turns_left=max(0, MAX_QUESTIONS - self.asked),
        )
        turn = await call_json(prompt, _Turn, settings.model_fast, reasoning=INTAKE_REASONING)

        medical = turn.medical_question or bool(_MEDICAL_RE.search(lowered))
        self._fuzzy_city = None
        filled = self._merge(turn.updates, lowered, medical)
        filled = list(dict.fromkeys(filled + self._mark_answered(turn, lowered, asked, medical)))

        default = ""
        if asked is not None and not self.is_answered(asked) and not medical:
            self.misses[asked] += 1
            if self.misses[asked] >= 2:
                # Asked twice with no usable answer: use the default, say so, move on.
                self.answered.add(asked)
                default = self._lines[f"default_{asked}"] + " "
                log.info("intake used the default for %s after two unusable answers", asked)

        missing = self.missing()
        important = [t for t in IMPORTANT if t in missing]
        allowed = important or missing
        lead = self._lines["no_advice"] + " " if medical else ""
        # A question repeated after the no-advice line is the same question, not a new one.
        repeat = medical and asked in missing
        out_of_turns = self.asked >= MAX_QUESTIONS and not repeat
        model_done = turn.done and not important and not medical
        if not missing or finish or model_done or out_of_turns:
            self.done = True
            self.current = None
            self.answered.update(missing)  # the rest takes its defaults
            spoken = lead + default + self._confirmation()
            self.history.append(("lana", spoken))
            # The confirmation names the place, so a fuzzy city needs no separate check.
            return IntakeTurn(filled=filled, done=True, spoken=spoken)

        reply = turn.reply.strip().strip('"').strip()
        problem = None if turn.asking in allowed else f"asks about {turn.asking!r}, allowed {allowed}"
        problem = problem or reply_problem(reply, previous)
        from_model = False
        if repeat:
            topic, question = asked, self._fixed(asked, None)
        elif problem is None and not medical:
            topic, question, from_model = turn.asking, reply, True
        else:
            if not medical:
                log.info("intake fell back to the fixed question (%s): %r", problem, reply)
            topic = allowed[0]
            question = self._fixed(topic, previous)
        if not repeat:
            self.asked += 1
        self.current = topic

        if self._fuzzy_city:
            lead += self._lines["fuzzy_city"].format(city=self._fuzzy_city) + " "
        if (lead or default) and from_model:
            question = _question_part(reply)  # Python's own line is the acknowledgement
        spoken = lead + default + question
        if previous is not None and _norm(spoken) == _norm(previous):
            question = self._lines[f"again_{topic}"]
            spoken = lead + default + question
        self.history.append(("lana", spoken))
        return IntakeTurn(filled=filled, next_question=question, spoken=spoken)

    def _mark_answered(self, turn: _Turn, text: str, asked: str | None, medical: bool) -> list[str]:
        """Topics the patient addressed without a value: the model's list, "near me", "nothing", "skip"."""
        filled: list[str] = []
        for topic in turn.answered:
            # "Do I need braces?" addresses no topic.
            if topic in TOPIC_FIELDS and not (medical and topic == "treatment"):
                self.answered.add(topic)

        if _NEAR_ME_RE.search(text):
            if turn.updates.max_distance_mi is None:
                self.draft.max_distance_mi = NEAR_ME_DISTANCE_MI
                filled.append("max_distance_mi")
            musts = self.draft.must_haves or []
            if NEAR_ME_MUST not in musts:
                self.draft.must_haves = [*musts, NEAR_ME_MUST]
                filled.append("must_haves")
            self.answered.add("must_haves")
        elif (asked == "must_haves" or "must_haves" in turn.answered) and not turn.updates.must_haves:
            if _NOTHING_RE.search(text) or "must_haves" in turn.answered:
                if self.draft.must_haves is None:
                    self.draft.must_haves = []
                    filled.append("must_haves")
                self.answered.add("must_haves")

        if asked is not None and not medical and not self.is_answered(asked) and _SKIP_RE.search(text):
            self.answered.add(asked)
        return filled

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
