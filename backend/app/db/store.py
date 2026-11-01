"""In-memory case store. Cases expire 30 minutes after their last use; nothing is written to disk."""

import time
from dataclasses import dataclass, field

from app.schemas.case import Case

CASE_TTL_S = 30 * 60


@dataclass
class CaseRecord:
    case: Case
    voice: bool
    clinics: int
    touched: float = field(default_factory=time.monotonic)
    runs: int = 0
    seq: int = 0  # last seq sent; one counter per case across all event types

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class CaseStore:
    def __init__(self) -> None:
        self._records: dict[str, CaseRecord] = {}

    def create(self, case: Case, voice: bool, clinics: int) -> CaseRecord:
        self.purge()
        record = CaseRecord(case=case, voice=voice, clinics=clinics)
        self._records[case.id] = record
        return record

    def get(self, case_id: str) -> CaseRecord | None:
        """The record, or None if it doesn't exist or has expired. Refreshes its expiry."""
        self.purge()
        record = self._records.get(case_id)
        if record is not None:
            record.touched = time.monotonic()
        return record

    def purge(self) -> None:
        now = time.monotonic()
        for case_id in [k for k, r in self._records.items() if now - r.touched > CASE_TTL_S]:
            del self._records[case_id]

    def clear(self) -> None:
        self._records.clear()


store = CaseStore()
