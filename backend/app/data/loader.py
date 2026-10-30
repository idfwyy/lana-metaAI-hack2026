import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.schemas.clinic import Clinic, PriceSheet

DEMO_CLINICS_PATH = Path(__file__).parent / "demo_clinics.json"
DEMO_TZ = ZoneInfo("America/Chicago")
SLOT_WINDOW_DAYS = 10
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _weekday_offset(weekday: str, today: date, nth: int) -> int:
    """Days from today to the nth (0-based) upcoming `weekday`, never today itself.

    If that one falls past the 10-day window, fall back to an earlier one so every slot stays in the window.
    """
    target = WEEKDAYS.index(weekday.lower())
    offset = (target - today.weekday() - 1) % 7 + 1 + 7 * nth
    while offset > SLOT_WINDOW_DAYS and offset - 7 >= 1:
        offset -= 7
    return offset


def _build_slots(slots: list[dict], today: date) -> list[datetime]:
    """Turn {"day_offset": 3, "time": "18:00"} or {"weekday": "saturday", "time": "10:00"} into aware datetimes.

    ZoneInfo picks the right offset across DST changes.
    """
    weekday_counts: dict[str, int] = {}
    result = []
    for slot in slots:
        if "weekday" in slot:
            nth = weekday_counts.get(slot["weekday"], 0)
            weekday_counts[slot["weekday"]] = nth + 1
            offset = _weekday_offset(slot["weekday"], today, nth)
        else:
            offset = slot["day_offset"]
        day = today + timedelta(days=offset)
        result.append(datetime.combine(day, time.fromisoformat(slot["time"]), tzinfo=DEMO_TZ))
    return result


def load_demo_clinics(today: date | None = None) -> list[tuple[Clinic, PriceSheet]]:
    """Load every demo clinic with its hidden price sheet. Only the simulator should need the sheets.

    Slots are rebuilt from today's date (in America/Chicago) on every call, so they stay in the next 10 days.
    """
    today = today or datetime.now(DEMO_TZ).date()
    raw = json.loads(DEMO_CLINICS_PATH.read_text(encoding="utf-8"))
    result = []
    for entry in raw:
        sheet = dict(entry["price_sheet"])
        sheet["open_slots"] = _build_slots(sheet["open_slots"], today)
        result.append((Clinic.model_validate(entry["clinic"]), PriceSheet.model_validate(sheet)))
    return result


def get_public_clinics() -> list[Clinic]:
    """Public clinic info only, safe to show the agent. No price data."""
    return [clinic for clinic, _ in load_demo_clinics()]
