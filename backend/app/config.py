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

# Every field Lana can ask on a call (agent/extractor.py CHECKLIST_ORDER), for DEMO_CHECKLIST.
CHECKLIST_FIELDS = (
    "total_price", "records_fee", "includes", "monthly_payment", "down_payment", "next_slots", "evening_hours",
)


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
    # The model and reasoning level for Lana's intake turns. A blank model means MODEL_FAST.
    # Picked by testing; see docs/decisions.md.
    intake_model: str = ""
    intake_reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max"] = "low"

    # Voice
    elevenlabs_api_key: SecretStr = SecretStr("")
    # TTS model per speaker. A blank one falls back to ELEVENLABS_MODEL, then to the default
    # (see _tts_models below).
    elevenlabs_model: str = ""
    elevenlabs_model_agent: str = ""
    elevenlabs_model_clinic: str = ""
    # Audio tag put before Lana's lines when her model performs tags. Never shown on screen.
    elevenlabs_tag_agent: str = "[warmly]"
    # Voice settings sent with every line (and part of the TTS cache key).
    elevenlabs_stability: float = Field(default=0.4, ge=0, le=1)
    elevenlabs_similarity: float = Field(default=0.8, ge=0, le=1)
    elevenlabs_style: float = Field(default=0.3, ge=0, le=1)
    elevenlabs_speaker_boost: bool = True
    agent_name: str = "Lana"
    voice_agent: str = ""
    voice_clinic_1: str = ""
    voice_clinic_2: str = ""
    voice_clinic_3: str = ""
    # The demo patient in Watch mode. Blank falls back to VOICE_CLINIC_1.
    voice_patient: str = ""

    # Turn taking: after Meta ends a turn, wait this long in case the patient keeps talking.
    end_grace_ms: int = Field(default=700, ge=0, le=5000)
    # Be the clinic: seconds of silence before Lana says "Sorry, I didn't catch that." (a second
    # silence ends the call), and how long she waits for the judge to answer her hold request.
    judge_silence_s: float = Field(default=12, gt=0)
    judge_hold_s: float = Field(default=8, gt=0)

    # App
    timezone: str = "America/Chicago"
    # The web app's origins, comma-separated (e.g. https://lana-health.pages.dev,http://localhost:5173).
    web_origin: str = "http://localhost:5173"
    # Optional: more origins by regex, e.g. Cloudflare Pages previews https://[a-z0-9-]+\.lana-health\.pages\.dev
    web_origin_regex: str = ""

    # Access gate (app/access.py). Both come only from the environment; never commit real values.
    # ACCESS_CODES: comma-separated valid codes, one per person so one can be removed later. Empty
    # turns the gate off (local dev). ACCESS_TOKEN_SECRET signs the passes; the gate refuses
    # everyone while codes are set without it.
    access_codes: SecretStr = SecretStr("")
    access_token_secret: SecretStr = SecretStr("")

    # Demo usage limits (app/guard.py). TTS characters count only what is sent to ElevenLabs;
    # STT seconds count only real mic audio sent to Meta (no session is open while Lana talks).
    max_runs_per_case: int = Field(default=2, ge=1)
    # A judge trying all three modes uses up to 4 cases.
    max_cases_per_ip_hour: int = Field(default=20, ge=1)
    max_llm_calls_per_case: int = Field(default=60, ge=1)
    max_tts_chars_per_case: int = Field(default=4000, ge=0)
    max_tts_chars_per_day: int = Field(default=30000, ge=0)
    max_stt_seconds_per_case: int = Field(default=300, ge=1)

    # The checklist fields Lana asks on a call, comma-separated, in agent/extractor.py order.
    # The extractor still fills any other field the clinic mentions.
    demo_checklist: str = "total_price,records_fee,monthly_payment,next_slots"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @field_validator("demo_checklist")
    @classmethod
    def _known_fields(cls, v: str) -> str:
        names = [n.strip() for n in v.split(",") if n.strip()]
        unknown = [n for n in names if n not in CHECKLIST_FIELDS]
        if unknown or not names:
            raise ValueError(f"DEMO_CHECKLIST needs names from {', '.join(CHECKLIST_FIELDS)}; got {v!r}")
        return ",".join(names)

    @model_validator(mode="after")
    def _tts_models(self) -> "Settings":
        self.elevenlabs_model_agent = self.elevenlabs_model_agent or self.elevenlabs_model or "eleven_v4_turbo"
        self.elevenlabs_model_clinic = self.elevenlabs_model_clinic or self.elevenlabs_model or "eleven_flash_v2_5"
        return self

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def demo_fields(self) -> frozenset[str]:
        return frozenset(self.demo_checklist.split(","))

    @property
    def web_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]

    @property
    def access_code_list(self) -> list[str]:
        """The valid codes, trimmed and lowercased (codes are not case-sensitive); blanks dropped."""
        raw = self.access_codes.get_secret_value()
        return [c for c in (part.strip().lower() for part in raw.split(",")) if c]

    def missing_keys(self) -> list[str]:
        """Names (never values) of the API keys the server can't work without that are blank."""
        keys = {"META_API_KEY": self.meta_api_key, "ELEVENLABS_API_KEY": self.elevenlabs_api_key}
        return [name for name, value in keys.items() if not value.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
