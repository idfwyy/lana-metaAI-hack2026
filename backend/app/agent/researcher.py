"""Picks the clinics to call and the call order. Plain Python, no AI.

Only public clinic info is used. Price is unknown at this point, so the order is fit with the
patient's must-haves first, then rating, then distance.
"""

import re
from collections.abc import Callable

from app.config import MISSING_SUBSCORE
from app.data.loader import get_public_clinics
from app.schemas.clinic import Clinic
from app.schemas.preferences import Preferences

EVENING_CLOSE_MINUTES = 18 * 60  # closing at 6pm or later counts as evening hours

# "8am-7pm", "9:30am - 5:30pm"
_RANGE_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE
)
_WEEKEND_RE = re.compile(r"\b(?:sat|sun)", re.IGNORECASE)

OnLog = Callable[[str], None]


def _minutes(hour: str, minute: str | None, half: str) -> int:
    h = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    return h * 60 + int(minute or 0)


def _has_evening_hours(hours: str) -> bool:
    return any(_minutes(m[4], m[5], m[6]) >= EVENING_CLOSE_MINUTES for m in _RANGE_RE.finditer(hours))


def _must_have_score(must: str, clinic: Clinic) -> float:
    """1 or 0 when the public hours answer it, MISSING_SUBSCORE when they can't."""
    text = must.lower()
    if "evening" in text or "after work" in text:
        return 1.0 if _has_evening_hours(clinic.hours) else 0.0
    if "saturday" in text or "weekend" in text:
        return 1.0 if _WEEKEND_RE.search(clinic.hours) else 0.0
    return MISSING_SUBSCORE


def fit_score(clinic: Clinic, preferences: Preferences) -> float:
    if not preferences.must_haves:
        return 1.0
    scores = [_must_have_score(m, clinic) for m in preferences.must_haves]
    return sum(scores) / len(scores)


def _short(clinic: Clinic) -> str:
    return clinic.name.split()[0]


def _reason(first: Clinic, rest: list[Clinic], preferences: Preferences) -> str:
    met = [m for m in preferences.must_haves if _must_have_score(m, first) == 1.0]
    best_fit = fit_score(first, preferences)
    if met and all(best_fit > fit_score(c, preferences) for c in rest):
        return f"best fit for {', '.join(met)}"
    if not rest:
        return "the only one in range"
    if all(first.rating > c.rating for c in rest):
        return "highest rated"
    return "closest"


def pick_clinics(
    preferences: Preferences, max_clinics: int = 3, on_log: OnLog | None = None
) -> list[Clinic]:
    """Clinics within the patient's distance, best fit first. Ties go to the alphabetically first id."""
    log = on_log or (lambda _: None)
    distance = preferences.max_distance_mi
    miles = f"{distance:g} mile{'s' if distance != 1 else ''}"
    in_range = [c for c in get_public_clinics() if c.distance_mi <= preferences.max_distance_mi]
    if not in_range:
        log(f"No orthodontists found within {miles}")
        return []

    count = len(in_range)
    log(f"Found {count} orthodontist{'s' if count != 1 else ''} within {miles}")
    in_range.sort(key=lambda c: (-fit_score(c, preferences), -c.rating, c.distance_mi, c.id))
    picked = in_range[:max_clinics]

    first, rest = picked[0], picked[1:]
    log(f"Calling {_short(first)} first: {_reason(first, rest, preferences)}")
    if rest:
        log("Then " + ", then ".join(_short(c) for c in rest))
    return picked
