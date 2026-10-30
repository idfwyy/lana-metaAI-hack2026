"""Plain Python ranking -> top 3 Offers. The AI only writes tradeoff lines afterwards.

score = weighted sum of sub-scores, each 0 to 1 (weights in config.RANKING_WEIGHTS):
- price: cheapest all-in among the kept quotes / this all-in
- fit: share of the patient's must-haves the clinic confirmed ("evening hours" maps to the
  evening_hours answer; any other must-have can't be checked yet and counts as missing)
- rating: rating / 5
- distance: 1 - distance / max distance
- availability: 1 - days to the earliest open slot / 10
Missing data scores MISSING_SUBSCORE.
"""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.config import MISSING_SUBSCORE, RANKING_WEIGHTS, get_settings
from app.integrations.meta_llm import call_json
from app.schemas.clinic import Clinic
from app.schemas.negotiation import NegotiationResult
from app.schemas.offer import Offer
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "ranker.md"

MAX_OFFERS = 3
AVAILABILITY_WINDOW_DAYS = 10


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def all_in(quote: Quote, negotiation: NegotiationResult | None = None) -> int | None:
    """The negotiated price when there is one, else the quoted all-in price."""
    return negotiation.final_all_in if negotiation is not None else quote.all_in_price


def passes_filters(
    quote: Quote, clinic: Clinic, prefs: Preferences, price: int | None, check_budget: bool = True
) -> bool:
    """Hard filters. Unknown values pass: only a confirmed answer can rule a clinic out."""
    if check_budget and price is not None and price > prefs.budget_max:
        return False
    if clinic.distance_mi > prefs.max_distance_mi:
        return False
    plan = quote.checklist.payment_plan
    if prefs.prefers_monthly and plan is not None and plan.value is False:
        return False
    return True


def _fit(quote: Quote, prefs: Preferences) -> float:
    if not prefs.must_haves:
        return 1.0
    scores = []
    for must in prefs.must_haves:
        evening = quote.checklist.evening_hours
        if "evening" in must.lower() and evening is not None:
            scores.append(1.0 if evening.value else 0.0)
        else:
            scores.append(MISSING_SUBSCORE)
    return sum(scores) / len(scores)


def _availability(quote: Quote, now: datetime) -> float:
    slots = quote.checklist.next_slots
    if slots is None:
        return MISSING_SUBSCORE
    if not slots.value:
        return 0.0
    days = (min(slots.value).astimezone(now.tzinfo).date() - now.date()).days
    return _clamp(1 - days / AVAILABILITY_WINDOW_DAYS)


def sub_scores(
    quote: Quote,
    clinic: Clinic,
    prefs: Preferences,
    price: int | None,
    cheapest: int | None,
    now: datetime,
) -> dict[str, float]:
    return {
        "price": MISSING_SUBSCORE if price is None or not cheapest else _clamp(cheapest / price),
        "fit": _fit(quote, prefs),
        "rating": _clamp(clinic.rating / 5),
        "distance": _clamp(1 - clinic.distance_mi / prefs.max_distance_mi),
        "availability": _availability(quote, now),
    }


def weighted(scores: dict[str, float], skip: str | None = None) -> float:
    """Weighted sum; with `skip`, that weight is dropped and the rest rescaled to sum to 1."""
    weights = {name: getattr(RANKING_WEIGHTS, name) for name in scores if name != skip}
    total = sum(weights.values())
    return _clamp(sum(scores[name] * w for name, w in weights.items()) / total)


def rank_offers(
    quotes: list[Quote],
    clinics: list[Clinic],
    preferences: Preferences,
    negotiations: list[NegotiationResult] | None = None,
    now: datetime | None = None,
) -> list[Offer]:
    """Up to 3 Offers, best first. Ties break on clinic id so the order is always the same."""
    now = now or datetime.now(get_settings().tz)
    by_id = {c.id: c for c in clinics}
    deals = {n.clinic_id: n for n in negotiations or []}

    kept = []
    for quote in quotes:
        clinic = by_id.get(quote.clinic_id)
        if clinic is None:
            continue
        price = all_in(quote, deals.get(quote.clinic_id))
        if passes_filters(quote, clinic, preferences, price):
            kept.append((quote, clinic, price))

    prices = [price for _, _, price in kept if price is not None]
    cheapest = min(prices) if prices else None
    scored = [
        (weighted(sub_scores(quote, clinic, preferences, price, cheapest, now)), quote, clinic)
        for quote, clinic, price in kept
    ]
    scored.sort(key=lambda item: (-item[0], item[2].id))

    return [
        Offer(
            rank=i,
            clinic=clinic,
            quote=quote,
            negotiation=deals.get(clinic.id),
            score=round(score, 4),
            unknowns=list(quote.missing_fields),
        )
        for i, (score, quote, clinic) in enumerate(scored[:MAX_OFFERS], start=1)
    ]


class _OfferTradeoffs(BaseModel):
    clinic_id: str
    tradeoffs: list[str]


class _Tradeoffs(BaseModel):
    offers: list[_OfferTradeoffs]


def _offer_facts(offer: Offer) -> str:
    q = offer.quote.checklist
    price = all_in(offer.quote, offer.negotiation)
    facts = [
        f"clinic_id: {offer.clinic.id}",
        f"rank: {offer.rank}",
        f"name: {offer.clinic.name}",
        f"all-in price: {f'${price:,}' if price is not None else 'not confirmed'}",
        f"distance: {offer.clinic.distance_mi:g} miles",
        f"rating: {offer.clinic.rating:g} ({offer.clinic.review_count} reviews)",
        f"hours: {offer.clinic.hours}",
        f"evening appointments: {'not confirmed' if q.evening_hours is None else q.evening_hours.value}",
        f"monthly: {'not confirmed' if q.monthly_payment is None else f'${q.monthly_payment.value:,}'}",
        f"not confirmed: {', '.join(offer.unknowns) or 'nothing'}",
    ]
    return "\n".join(f"- {fact}" for fact in facts)


async def add_tradeoffs(offers: list[Offer]) -> list[Offer]:
    """Ask MODEL_FAST for 1 to 2 tradeoff lines per offer. The order never changes."""
    if not offers:
        return offers
    settings = get_settings()
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        offers="\n\n".join(_offer_facts(o) for o in offers)
    )
    reply = await call_json(prompt, _Tradeoffs, settings.model_fast, reasoning=settings.call_reasoning)
    lines = {item.clinic_id: [t.strip() for t in item.tradeoffs if t.strip()][:2] for item in reply.offers}
    result = [o.model_copy(update={"tradeoffs": lines.get(o.clinic.id, [])}) for o in offers]
    assert [o.clinic.id for o in result] == [o.clinic.id for o in offers], "tradeoffs changed the order"
    return result
