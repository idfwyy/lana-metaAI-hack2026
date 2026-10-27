from enum import StrEnum


class CaseStatus(StrEnum):
    """Orchestrator states. Allowed moves live in agent/orchestrator.py."""

    NEW = "new"
    INTAKE = "intake"
    PLAN_CHECK = "plan_check"
    RESEARCH = "research"
    CALLING = "calling"
    HOLDING = "holding"
    QUOTES_IN = "quotes_in"
    NEGOTIATING = "negotiating"
    OFFERS_READY = "offers_ready"
    CALLBACK = "callback"
    BOOKED = "booked"
    SAVED = "saved"
    ERROR = "error"


class CallStatus(StrEnum):
    DIALING = "dialing"
    LIVE = "live"
    ON_HOLD = "on_hold"
    NEGOTIATING = "negotiating"
    ENDED = "ended"


class Speaker(StrEnum):
    AGENT = "agent"  # Lana
    CLINIC = "clinic"  # demo receptionist, judge, or real phone line
    PATIENT = "patient"


class DemoMode(StrEnum):
    WATCH = "watch"
    BE_THE_CLINIC = "be_the_clinic"
    REAL_CALL = "real_call"


class Treatment(StrEnum):
    BRACES = "braces"
    ALIGNERS = "aligners"
    NOT_SURE = "not sure"
