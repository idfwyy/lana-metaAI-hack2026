"""Demo clinic receptionist: Muse Spark plays the front desk with a hidden price sheet.

Only this module reads a PriceSheet. The floor price is enforced here in Python,
not just in the prompt.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.integrations.meta_llm import stream_chat
from app.schemas.clinic import Clinic, PriceSheet

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "receptionist.md"

HOLD_LINE = "Sure, no problem, I'll hold."
FALLBACK_LINE = "I'm sorry, I can't go that low on the price."
REPEAT_LINE = "Sorry, could you say that again?"
CORRECTION = (
    "\n\nCorrection: your last reply was \"{reply}\". {problem} Reply again with 1 or 2 "
    "sentences that keep every price at or above ${floor:,}, written as digits with a $ sign, "
    "or politely say you can't go that low."
)

# Amounts from this share of the floor up to the floor count as a price that breaks it.
# Smaller amounts are fees, monthly payments or discounts ($180 a month, a $250 records fee).
PRICE_SIZED_FRACTION = 0.5

_AMOUNT_RE = re.compile(
    r"(?P<dollar>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<mult>k|thousand)\b)?(?:\s?(?P<word>dollars|bucks)\b)?",
    re.IGNORECASE,
)

# An amount in thousands written in words ("three thousand", "a thousand"), which
# dollar_amounts can't read, so it fails the check.
_SPELLED_THOUSANDS_RE = re.compile(
    r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred)[\s-]+thousand\b",
    re.IGNORECASE,
)

# A request to wait on the line, not "hold that slot".
_HOLD_RE = re.compile(
    r"\bon hold\b|\bhold (?:the line|on)\b"
    r"|\bhold\b(?=\s*(?:[.?!,]|$|for\b|while\b|(?:a|one) (?:moment|minute|sec)))",
    re.IGNORECASE,
)


def dollar_amounts(text: str) -> list[int]:
    """Whole-dollar amounts said in a line: "$4,800", "$3.5k", "3,990", "4000 dollars"."""
    amounts = []
    for m in _AMOUNT_RE.finditer(text):
        num = m["num"]
        if not (m["dollar"] or m["word"] or "," in num):
            continue
        value = float(num.replace(",", ""))
        if m["mult"]:
            value *= 1000
        amounts.append(round(value))
    return amounts


def is_hold_request(text: str) -> bool:
    return _HOLD_RE.search(text) is not None


def _money(amount: int) -> str:
    return f"${amount:,}"


def _slot(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{dt:%A, %B} {dt.day} at {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _price_sheet_text(clinic: Clinic, sheet: PriceSheet) -> str:
    consult = "free" if sheet.consult_fee == 0 else _money(sheet.consult_fee)
    records = "none"
    if sheet.records_fee:
        records = f"{_money(sheet.records_fee)}, not included in the treatment price"
    slots = "; ".join(_slot(dt) for dt in sheet.open_slots) or "none this week"
    return "\n".join(
        [
            f"- Office hours: {clinic.hours}",
            f"- Comprehensive orthodontic treatment: {_money(sheet.list_price)}",
            f"- The treatment price includes: {', '.join(sheet.includes) or 'nothing extra'}",
            f"- Records fee (x-rays and scans): {records}",
            f"- Consult: {consult}",
            f"- Monthly plan: {_money(sheet.monthly)} a month with "
            f"{_money(sheet.down_payment)} down",
            f"- Open consult slots: {slots}",
        ]
    )


def _concessions_text(sheet: PriceSheet) -> str:
    lines = []
    if sheet.will_price_match:
        lines.append(
            "- Price match: you may match a lower all-in quote from another clinic "
            "that Lana names, but never below your lowest price."
        )
    else:
        lines.append("- Price match: not allowed. You do not match other clinics' prices.")
    if sheet.will_waive_records and sheet.records_fee:
        lines.append(f"- Waive the {_money(sheet.records_fee)} records fee: allowed.")
    else:
        lines.append("- Waive the records fee: not allowed.")
    if sheet.pay_in_full_discount_pct:
        off = round(sheet.list_price * sheet.pay_in_full_discount_pct / 100)
        lines.append(
            f"- Pay in full: {sheet.pay_in_full_discount_pct:g}% off the treatment price "
            f"({_money(off)} off {_money(sheet.list_price)}) if the patient pays it all up front."
        )
    else:
        lines.append("- Pay-in-full discount: not allowed.")
    if sheet.will_add_retainers:
        lines.append("- An extra set of retainers at no charge: allowed.")
    else:
        lines.append("- Extra retainers at no charge: not allowed.")
    return "\n".join(lines)


class ClinicSimulator:
    """One demo receptionist on one call. Keeps its own message history."""

    def __init__(self, clinic: Clinic, price_sheet: PriceSheet) -> None:
        self.clinic = clinic
        self.sheet = price_sheet
        self.on_hold = False
        self._history: list[dict] = []
        settings = get_settings()
        today = datetime.now(settings.tz)
        self._system = PROMPT_PATH.read_text(encoding="utf-8").format(
            clinic_name=clinic.name,
            personality=price_sheet.personality,
            today=f"{today:%A, %B} {today.day}",
            price_sheet=_price_sheet_text(clinic, price_sheet),
            concessions=_concessions_text(price_sheet),
            floor_price=price_sheet.floor_price,
        )

    @property
    def history(self) -> list[dict]:
        """Lana's lines (user) and the receptionist's replies (assistant), in order."""
        return list(self._history)

    async def next_line(self, lana_line: str) -> str:
        """The receptionist's reply to Lana's line."""
        self._history.append({"role": "user", "content": lana_line})
        if is_hold_request(lana_line):
            self.on_hold = True
            reply = HOLD_LINE
        else:
            self.on_hold = False
            reply = await self._safe_reply()
        self._history.append({"role": "assistant", "content": reply})
        return reply

    async def _safe_reply(self) -> str:
        reply = await self._generate(self._system)
        problem = self._price_problem(reply)
        if problem is None:
            return reply or REPEAT_LINE

        log.warning(
            "simulator clinic=%s failed price check, regenerating: %s", self.clinic.id, problem
        )
        correction = CORRECTION.format(reply=reply, problem=problem, floor=self.sheet.floor_price)
        reply = await self._generate(self._system + correction)
        problem = self._price_problem(reply)
        if problem is None:
            return reply or REPEAT_LINE

        log.warning(
            "simulator clinic=%s failed price check again, using fallback: %s",
            self.clinic.id,
            problem,
        )
        return FALLBACK_LINE

    async def _generate(self, system: str) -> str:
        settings = get_settings()
        messages = [{"role": "system", "content": system}, *self._history]
        stream = stream_chat(messages, settings.model_fast, reasoning=settings.call_reasoning)
        chunks = [c async for c in stream]
        # No em dashes in on-screen text, same cleanup as Lana's lines.
        return re.sub(r"\s*—\s*", ", ", "".join(chunks)).strip()

    def _price_problem(self, reply: str) -> str | None:
        """Why the reply fails the price check, or None if it passes."""
        floor = self.sheet.floor_price
        for amount in dollar_amounts(reply):
            if floor * PRICE_SIZED_FRACTION <= amount < floor:
                return f"It said ${amount:,}, which is below the lowest price you may say."
        spelled = _SPELLED_THOUSANDS_RE.search(reply)
        if spelled:
            return f'It wrote an amount in words ("{spelled[0]}").'
        return None
