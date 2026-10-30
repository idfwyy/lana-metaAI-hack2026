"""Ranker with the LLM mocked. No network."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.agent import ranker
from app.agent.ranker import add_tradeoffs, rank_offers, sub_scores
from app.config import MISSING_SUBSCORE, get_settings
from app.data.loader import get_public_clinics
from app.schemas.checklist import ClinicChecklist
from app.schemas.enums import Treatment
from app.schemas.negotiation import NegotiationResult
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "three_quotes.json"
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=get_settings().tz)


def _prefs(**overrides) -> Preferences:
    data = dict(
        treatment=Treatment.BRACES,
        zip_code="76010",
        max_distance_mi=15,
        budget_max=5000,
        pay_in_full_ok=True,
        prefers_monthly=True,
        must_haves=["evening hours"],
    )
    return Preferences(**{**data, **overrides})


def _quotes() -> list[Quote]:
    return [Quote.model_validate(q) for q in json.loads(FIXTURE.read_text(encoding="utf-8"))]


def _ids(offers) -> list[str]:
    return [o.clinic.id for o in offers]


def test_order_is_deterministic() -> None:
    clinics = get_public_clinics()
    prefs = _prefs(budget_max=6000)

    runs = [_ids(rank_offers(_quotes(), clinics, prefs, now=NOW)) for _ in range(3)]
    shuffled = _ids(rank_offers(list(reversed(_quotes())), list(reversed(clinics)), prefs, now=NOW))

    assert runs[0] == runs[1] == runs[2] == shuffled
    assert runs[0] == ["parkway", "sunrise", "lakeside"]
    offers = rank_offers(_quotes(), clinics, prefs, now=NOW)
    assert [o.rank for o in offers] == [1, 2, 3]
    assert offers[0].score >= offers[1].score >= offers[2].score


def test_over_budget_clinic_filtered_out() -> None:
    offers = rank_offers(_quotes(), get_public_clinics(), _prefs(budget_max=5000), now=NOW)

    assert "parkway" not in _ids(offers)  # $5,050 all in
    assert _ids(offers) == ["sunrise", "lakeside"]


def test_negotiated_price_is_used() -> None:
    deal = NegotiationResult(clinic_id="parkway", first_all_in=5050, final_all_in=4200)

    offers = rank_offers(_quotes(), get_public_clinics(), _prefs(), negotiations=[deal], now=NOW)

    assert _ids(offers)[0] == "parkway"
    assert offers[0].negotiation == deal


def test_distance_and_no_plan_filters() -> None:
    quotes = _quotes()
    sunrise = next(q for q in quotes if q.clinic_id == "sunrise")
    sunrise.checklist.payment_plan = sunrise.checklist.monthly_payment.model_copy(
        update={"value": False}
    )

    offers = rank_offers(quotes, get_public_clinics(), _prefs(max_distance_mi=6), now=NOW)

    assert _ids(offers) == []  # parkway over budget, sunrise has no plan, lakeside is 8 mi


def test_ties_break_on_clinic_id() -> None:
    clinic = get_public_clinics()[0]
    twins = [clinic.model_copy(update={"id": cid}) for cid in ("b", "a", "c")]
    quotes = [Quote(clinic_id=c.id, checklist=ClinicChecklist()) for c in twins]

    assert _ids(rank_offers(quotes, twins, _prefs(), now=NOW)) == ["a", "b", "c"]


def test_missing_data_scores_half() -> None:
    clinic = get_public_clinics()[0]
    quote = Quote(clinic_id=clinic.id, checklist=ClinicChecklist())

    scores = sub_scores(quote, clinic, _prefs(), None, 4200, NOW)

    assert scores["price"] == scores["fit"] == scores["availability"] == MISSING_SUBSCORE


def test_unpriced_quote_is_kept_with_unknowns() -> None:
    clinic = get_public_clinics()[0]
    quote = Quote(clinic_id=clinic.id, checklist=ClinicChecklist(), missing_fields=["total_price"])

    offers = rank_offers([quote], [clinic], _prefs(), now=NOW)

    assert _ids(offers) == [clinic.id]
    assert offers[0].unknowns == ["total_price"]


def test_at_most_three_offers() -> None:
    clinic = get_public_clinics()[0]
    clinics = [clinic.model_copy(update={"id": f"c{i}"}) for i in range(5)]
    quotes = [Quote(clinic_id=c.id, checklist=ClinicChecklist()) for c in clinics]

    assert len(rank_offers(quotes, clinics, _prefs(), now=NOW)) == 3


async def test_tradeoffs_never_change_order(monkeypatch: pytest.MonkeyPatch) -> None:
    offers = rank_offers(_quotes(), get_public_clinics(), _prefs(budget_max=6000), now=NOW)
    before = _ids(offers)

    async def fake_json(prompt, schema, model, images=None, reasoning=None):
        # The model answers in reverse order, with an extra line and a clinic that isn't ranked.
        items = [
            {"clinic_id": cid, "tradeoffs": [f"{cid} one", f"{cid} two", f"{cid} three"]}
            for cid in reversed(before)
        ]
        return schema.model_validate({"offers": [*items, {"clinic_id": "ghost", "tradeoffs": ["x"]}]})

    monkeypatch.setattr(ranker, "call_json", fake_json)

    result = await add_tradeoffs(offers)

    assert _ids(result) == before
    assert [o.rank for o in result] == [1, 2, 3]
    assert result[0].tradeoffs == [f"{before[0]} one", f"{before[0]} two"]
