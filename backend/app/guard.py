"""Demo usage limits: runs per case, new cases per IP, LLM calls, TTS characters and STT seconds.

The per-case counters live in a contextvar set by the server around a case run (and the
patient's voice session), so meta_llm.py, tts.py and main.py can charge them without
knowing about cases. Outside a case (the CLI, tests) charging does nothing.
"""

import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime

from app.config import get_settings

log = logging.getLogger(__name__)

LIMIT_MESSAGE = "This demo has a usage limit. Please try again later."
IP_WINDOW_S = 60 * 60


class LimitHit(Exception):
    """A usage limit was reached; the run stops."""


@dataclass
class CaseUsage:
    case_id: str
    llm_calls: int = 0
    tts_chars: int = 0
    stt_seconds: float = 0.0  # real mic audio sent to Meta STT
    hit: str | None = None  # "llm", "tts" or "stt" once a limit is reached
    on_limit: Callable[[], object] | None = None  # cancels the run


_usage: ContextVar[CaseUsage | None] = ContextVar("case_usage", default=None)
_day: date | None = None
_day_chars = 0
_ip_cases: dict[str, deque[float]] = {}


@contextmanager
def case_usage(usage: CaseUsage) -> Iterator[CaseUsage]:
    """Charge LLM calls and TTS characters to `usage` inside this block (and tasks created in it)."""
    token = _usage.set(usage)
    try:
        yield usage
    finally:
        _usage.reset(token)


def limit_hit() -> bool:
    usage = _usage.get()
    return usage is not None and usage.hit is not None


def _hit(usage: CaseUsage, reason: str) -> None:
    usage.hit = reason
    log.warning(
        "case=%s hit the %s limit (llm calls %d, tts chars %d, stt seconds %.1f)",
        usage.case_id, reason, usage.llm_calls, usage.tts_chars, usage.stt_seconds,
    )
    if usage.on_limit is not None:
        usage.on_limit()
    raise LimitHit(f"{reason} limit reached")


def charge_llm() -> None:
    """Count one LLM request for the current case. Raises LimitHit when the case is at its limit."""
    usage = _usage.get()
    if usage is None:
        return
    if usage.hit is not None or usage.llm_calls >= get_settings().max_llm_calls_per_case:
        _hit(usage, "llm")
    usage.llm_calls += 1


def charge_tts(chars: int) -> None:
    """Count characters sent to ElevenLabs. Raises LimitHit if the case or the day would go over."""
    global _day, _day_chars
    usage = _usage.get()
    if usage is None:
        return
    settings = get_settings()
    today = datetime.now(settings.tz).date()
    if today != _day:
        _day, _day_chars = today, 0
    if (
        usage.hit is not None
        or usage.tts_chars + chars > settings.max_tts_chars_per_case
        or _day_chars + chars > settings.max_tts_chars_per_day
    ):
        _hit(usage, "tts")
    usage.tts_chars += chars
    _day_chars += chars


def charge_stt(seconds: float) -> None:
    """Count seconds of real mic audio sent to Meta STT. Raises LimitHit once the case would go over its limit."""
    usage = _usage.get()
    if usage is None:
        return
    if usage.hit is not None or usage.stt_seconds + seconds > get_settings().max_stt_seconds_per_case:
        _hit(usage, "stt")
    usage.stt_seconds += seconds


def allow_new_case(ip: str) -> bool:
    """True (and counted) if this IP has created fewer than MAX_CASES_PER_IP_HOUR cases in the last hour."""
    now = time.monotonic()
    times = _ip_cases.setdefault(ip, deque())
    while times and now - times[0] > IP_WINDOW_S:
        times.popleft()
    if len(times) >= get_settings().max_cases_per_ip_hour:
        return False
    times.append(now)
    return True


def allow_run(runs_so_far: int) -> bool:
    return runs_so_far < get_settings().max_runs_per_case


def reset() -> None:
    """Forget the day counter and IP history (tests)."""
    global _day, _day_chars
    _day, _day_chars = None, 0
    _ip_cases.clear()
