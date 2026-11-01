"""Picks the clinics to call and the call order. Plain Python, no AI.

Only public clinic info is used: hours, rating, distance and the open consult times each
clinic's booking page shows. Price is unknown at this point, so the order is fit with the
patient's must-haves and preferred time first, then rating, then distance.
"""

import re
from collections.abc import Callable
from datetime import datetime

from app.agent.intake import CITIES
from app.config import MISSING_SUBSCORE, get_settings
from app.data.loader import get_public_clinics, public_openings
from app.schemas.clinic import Clinic
from app.schemas.preferences import Preferences

EVENING_CLOSE_MINUTES = 18 * 60  # closing at 6pm or later counts as evening hours
NOON_MINUTES = 12 * 60
# A slot from 5pm on is an evening slot; noon to 5pm is the afternoon.
EVENING_START_HOUR = 17

# "8am-7pm", "9:30am - 5:30pm"
_RANGE_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE
)
_WEEKEND_RE = re.compile(r"\b(?:sat|sun)", re.IGNORECASE)

OnLog = Callable[[str], None]


def _minutes(hour: str, minute: str | None, half: str) -> int:
    h = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    return h * 60 + int(minute or 0)


def _ranges(hours: str) -> list[tuple[int, int]]:
    return [(_minutes(m[1], m[2], m[3]), _minutes(m[4], m[5], m[6])) for m in _RANGE_RE.finditer(hours)]


def _has_evening_hours(hours: str) -> bool:
    return any(end >= EVENING_CLOSE_MINUTES for _, end in _ranges(hours))


def _hours_cover(hours: str, time_pref: str) -> bool:
    """The public hours include some of this time of day."""
    if time_pref == "weekend":
        return bool(_WEEKEND_RE.search(hours))
    if time_pref == "evening":
        return _has_evening_hours(hours)
    if time_pref == "morning":
        return any(start < NOON_MINUTES for start, _ in _ranges(hours))
    if time_pref == "afternoon":
        return any(start < EVENING_START_HOUR * 60 and end > NOON_MINUTES for start, end in _ranges(hours))
    return True


def slot_matches(slot: datetime, time_pref: str) -> bool:
    """The slot falls in the patient's preferred time. A Saturday 9 AM is a morning and a weekend."""
    if time_pref == "any":
        return True
    local = slot.astimezone(get_settings().tz)
    if time_pref == "weekend":
        return local.weekday() >= 5
    if time_pref == "morning":
        return local.hour < 12
    if time_pref == "afternoon":
        return 12 <= local.hour < EVENING_START_HOUR
    return local.hour >= EVENING_START_HOUR  # evening


def time_score(clinic: Clinic, time_pref: str, openings: list[datetime] | None = None) -> float:
    """Share of the clinic's listed openings in the preferred time. With none listed, the hours
    decide: MISSING_SUBSCORE if they cover that time, 0 if they don't."""
    if time_pref == "any":
        return 1.0
    if openings:
        return sum(slot_matches(s, time_pref) for s in openings) / len(openings)
    return MISSING_SUBSCORE if _hours_cover(clinic.hours, time_pref) else 0.0


def _must_have_score(must: str, clinic: Clinic) -> float:
    """1 or 0 when the public hours answer it, MISSING_SUBSCORE when they can't."""
    text = must.lower()
    if "evening" in text or "after work" in text:
        return 1.0 if _has_evening_hours(clinic.hours) else 0.0
    if "saturday" in text or "weekend" in text:
        return 1.0 if _WEEKEND_RE.search(clinic.hours) else 0.0
    for part in ("morning", "afternoon"):
        if part in text:
            return 1.0 if _hours_cover(clinic.hours, part) else 0.0
    return MISSING_SUBSCORE


def fit_score(clinic: Clinic, preferences: Preferences, openings: list[datetime] | None = None) -> float:
    scores = [_must_have_score(m, clinic) for m in preferences.must_haves]
    if preferences.time_pref != "any":
        scores.append(time_score(clinic, preferences.time_pref, openings))
    return sum(scores) / len(scores) if scores else 1.0


def _short(clinic: Clinic) -> str:
    return clinic.name.split()[0]


def place_name(zip_code: str) -> str:
    """The DFW city for one of intake's city ZIPs, else the ZIP itself."""
    return next((name for name, z in CITIES.items() if z == zip_code), zip_code)


def _reason(
    first: Clinic, rest: list[Clinic], preferences: Preferences, openings: dict[str, list[datetime]]
) -> str:
    """Why the first clinic is called first, from its public info: 'they fit best (evening hours, 4.9 stars)'."""
    stars = f"{first.rating:g} stars"
    pref = preferences.time_pref
    if pref != "any":
        own = time_score(first, pref, openings.get(first.id))
        if any(slot_matches(s, pref) for s in openings.get(first.id, [])) and all(
            own > time_score(c, pref, openings.get(c.id)) for c in rest
        ):
            return f"they have {pref} openings and {stars}"
    met = [m for m in preferences.must_haves if _must_have_score(m, first) == 1.0]
    best_fit = fit_score(first, preferences, openings.get(first.id))
    if met and all(best_fit > fit_score(c, preferences, openings.get(c.id)) for c in rest):
        return f"they fit best ({', '.join(met)}, {stars})"
    if not rest:
        return f"they're the only one in range ({stars})"
    if all(first.rating > c.rating for c in rest):
        closest = all(first.distance_mi < c.distance_mi for c in rest)
        return f"they're the highest rated ({stars}{f', {first.distance_mi:g} mi away' if closest else ''})"
    return f"they're closest ({first.distance_mi:g} mi)"


def pick_clinics(
    preferences: Preferences, max_clinics: int = 3, on_log: OnLog | None = None
) -> list[Clinic]:
    """Clinics within the patient's distance, best fit first. Ties go to the alphabetically first id."""
    log = on_log or (lambda _: None)
    distance = preferences.max_distance_mi
    miles = f"{distance:g} mile{'s' if distance != 1 else ''} of {place_name(preferences.zip_code)}"
    in_range = [c for c in get_public_clinics() if c.distance_mi <= preferences.max_distance_mi]
    if not in_range:
        log(f"No orthodontists found within {miles}")
        return []

    count = len(in_range)
    if preferences.place:
        # Outside DFW the same demo clinics stand in, placed near the patient's place; never real ones.
        log(f"Found {count} demo clinic{'s' if count != 1 else ''} near {preferences.place}")
    else:
        log(f"Found {count} orthodontist{'s' if count != 1 else ''} within {miles}")
    openings = public_openings()
    in_range.sort(key=lambda c: (-fit_score(c, preferences, openings.get(c.id)), -c.rating, c.distance_mi, c.id))
    picked = in_range[:max_clinics]

    # The browser reads the call order from "Calling X first" and "Then X, then Y" (web/src/lib/clinics.ts).
    first, rest = picked[0], picked[1:]
    log(f"Calling {_short(first)} first, {_reason(first, rest, preferences, openings)}")
    if rest:
        log("Then " + ", then ".join(_short(c) for c in rest))
    return picked
