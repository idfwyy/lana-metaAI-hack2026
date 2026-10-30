"""Settings from .env, ranking weights, and feature switches."""

from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root, next to .env.example.
REPO_ROOT = Path(__file__).resolve().parents[2]


class RankingWeights(BaseModel):
    """Weights for ranker.py. Each sub-score is 0 to 1; the weights must sum to 1."""

    model_config = ConfigDict(frozen=True)

    price: float = 0.40
    fit: float = 0.20
    rating: float = 0.15
    distance: float = 0.15
    availability: float = 0.10

    @model_validator(mode="after")
    def _sum_to_one(self) -> "RankingWeights":
        total = self.price + self.fit + self.rating + self.distance + self.availability
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"ranking weights must sum to 1, got {total}")
        return self


RANKING_WEIGHTS = RankingWeights()

# Sub-score used when a field is missing ("unknown stays unknown").
MISSING_SUBSCORE = 0.5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # Blank values in .env (as in .env.example) fall back to the defaults below.
        env_ignore_empty=True,
        extra="ignore",
    )

    # Meta Model API. Model IDs and the base URL come only from .env.
    meta_api_key: SecretStr = SecretStr("")
    meta_base_url: str = ""
    model_fast: str = ""
    model_smart: str = ""
    model_stt: str = ""
    stt_realtime_url: str = ""
    # reasoning_effort for live call turns. "none" is rejected by Muse models; "minimal" is lowest.
    call_reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max"] = "minimal"

    # Voice
    elevenlabs_api_key: SecretStr = SecretStr("")
    agent_name: str = "Lana"
    voice_agent: str = ""
    voice_clinic_1: str = ""
    voice_clinic_2: str = ""
    voice_clinic_3: str = ""

    # App
    database_url: str = "sqlite:///./data/dev.db"
    timezone: str = "America/Chicago"
    web_origin: str = "http://localhost:5173"
    public_base_url: str = ""

    # Safety switches
    enable_real_calls: bool = False
    allowed_call_numbers: str = Field(default="", description="Comma-separated E.164 numbers")

    # Stretch goal: Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_number: str = ""

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @cached_property
    def allowed_numbers(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.allowed_call_numbers.split(",") if n.strip())

    def may_call(self, number: str) -> bool:
        """Real calls only when switched on and the number is on the allow list."""
        return self.enable_real_calls and number.strip() in self.allowed_numbers


@lru_cache
def get_settings() -> Settings:
    return Settings()
