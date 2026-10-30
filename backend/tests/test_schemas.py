import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.data.loader import DEMO_CLINICS_PATH, get_public_clinics, load_demo_clinics
from app.schemas.clinic import Clinic, PriceSheet

CHICAGO = ZoneInfo("America/Chicago")


def test_every_clinic_and_price_sheet_validates():
    raw = json.loads(DEMO_CLINICS_PATH.read_text(encoding="utf-8"))
    assert len(raw) == 3
    for entry in raw:
        Clinic.model_validate(entry["clinic"])
        for slot in entry["price_sheet"]["open_slots"]:
            assert ("day_offset" in slot) != ("weekday" in slot)
            if "day_offset" in slot:
                assert 1 <= slot["day_offset"] <= 10
    assert len(load_demo_clinics()) == 3


def test_demo_clinic_fields():
    for clinic, sheet in load_demo_clinics():
        assert clinic.is_demo is True
        assert clinic.phone.startswith("555-01")
        assert clinic.voice_id is not None and clinic.voice_id.startswith("VOICE_CLINIC_")
        assert sheet.personality


def test_floor_price_not_above_list_price():
    for _, sheet in load_demo_clinics():
        assert sheet.floor_price <= sheet.list_price


def test_ids_are_unique():
    ids = [clinic.id for clinic, _ in load_demo_clinics()]
    assert len(ids) == len(set(ids))


def test_open_slots_are_chicago_time():
    for _, sheet in load_demo_clinics():
        assert len(sheet.open_slots) == 3
        for slot in sheet.open_slots:
            assert slot.tzinfo is not None
            assert slot.utcoffset() == slot.astimezone(CHICAGO).utcoffset()


def test_open_slots_are_in_the_next_10_days():
    now = datetime.now(CHICAGO)
    for _, sheet in load_demo_clinics():
        for slot in sheet.open_slots:
            assert slot > now
            assert 1 <= (slot.astimezone(CHICAGO).date() - now.date()).days <= 10


def test_open_slots_handle_dst_change():
    # Nov 1, 2026 is when Chicago falls back from CDT (-5) to CST (-6).
    sheets = {clinic.id: sheet for clinic, sheet in load_demo_clinics(today=date(2026, 10, 29))}
    sunrise_first = sheets["sunrise"].open_slots[0]  # day 1 -> Oct 30, before the change
    parkway_first = sheets["parkway"].open_slots[0]  # day 4 -> Nov 2, after the change
    assert sunrise_first.date() == date(2026, 10, 30)
    assert sunrise_first.utcoffset() == timedelta(hours=-5)
    assert parkway_first.date() == date(2026, 11, 2)
    assert parkway_first.utcoffset() == timedelta(hours=-6)
    assert (parkway_first.hour, parkway_first.minute) == (17, 30)


def test_lakeside_weekday_slots_land_on_saturday():
    raw = json.loads(DEMO_CLINICS_PATH.read_text(encoding="utf-8"))
    lakeside_raw = next(e for e in raw if e["clinic"]["id"] == "lakeside")
    weekday_idx = [i for i, s in enumerate(lakeside_raw["price_sheet"]["open_slots"]) if "weekday" in s]
    assert len(weekday_idx) == 2

    # Two weeks of start dates covers every weekday, including the Nov 1 clock change.
    for n in range(14):
        today = date(2026, 10, 25) + timedelta(days=n)
        sheets = {clinic.id: sheet for clinic, sheet in load_demo_clinics(today=today)}
        slots = [sheets["lakeside"].open_slots[i] for i in weekday_idx]
        next_saturday = today + timedelta(days=(5 - today.weekday() - 1) % 7 + 1)
        assert slots[0].date() == next_saturday
        for slot in slots:
            assert slot.weekday() == 5
            assert 1 <= (slot.date() - today).days <= 10
        expected_second = next_saturday + timedelta(days=7)
        if (expected_second - today).days > 10:
            expected_second = next_saturday
        assert slots[1].date() == expected_second


def test_public_clinics_have_no_price_data():
    clinics = get_public_clinics()
    assert len(clinics) == 3
    price_fields = set(PriceSheet.model_fields)
    for clinic in clinics:
        assert type(clinic) is Clinic
        assert not isinstance(clinic, PriceSheet)
        dumped = clinic.model_dump()
        assert price_fields.isdisjoint(dumped)
        assert not any(isinstance(v, PriceSheet) for v in dumped.values())
