"""ElevenLabs streaming text to speech, one voice per speaker, with a disk cache.

Every finished line is cached as an mp3 under backend/.cache/tts, so fixed lines (the
disclosure, the greeting, the intake questions) cost credits only once.

Never raises into the call loop: if ElevenLabs fails or times out, stream_speech logs
it and yields nothing, and the caller shows the text only.
"""

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

from elevenlabs import AsyncElevenLabs, VoiceSettings

from app.config import get_settings
from app.schemas.clinic import Clinic
from app.schemas.enums import Speaker

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "tts"
OUTPUT_FORMAT = "mp3_44100_128"
TIMEOUT_SECONDS = 8  # per request and per wait for the next chunk
READ_CHUNK_BYTES = 16 * 1024  # chunk size when streaming a cached file from disk

# Models that perform inline audio tags like [laughs]. Other models would read the tag aloud.
AUDIO_TAG_MODELS = frozenset({"eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo"})
_AUDIO_TAG_RE = re.compile(r"\s*\[[a-zA-Z][a-zA-Z ,'-]*\]\s*")


def strip_audio_tags(text: str) -> str:
    """'[warmly] Hi, I'm Lana. [laughs]' -> "Hi, I'm Lana." For any text shown on screen."""
    return re.sub(r"\s+([,.!?])", r"\1", _AUDIO_TAG_RE.sub(" ", text)).strip()


class VoiceConfigError(ValueError):
    """A voice ID (or the API key) needed for speech is blank or unknown."""


def voice_for(speaker: Speaker, clinic: Clinic | None = None) -> str:
    """The ElevenLabs voice ID for a speaker: VOICE_AGENT for Lana, the clinic's VOICE_CLINIC_n for a clinic."""
    settings = get_settings()
    if speaker == Speaker.AGENT:
        setting = "VOICE_AGENT"
    elif speaker == Speaker.CLINIC:
        if clinic is None:
            raise VoiceConfigError("A clinic voice needs the clinic, to read its voice_id")
        setting = clinic.voice_id
    else:
        raise VoiceConfigError(f"No voice for speaker '{speaker}'. Only Lana and clinics speak.")
    if not setting.startswith("VOICE_") or not hasattr(settings, setting.lower()):
        raise VoiceConfigError(f"Clinic '{clinic.id}' has voice_id '{setting}', which is not a voice setting")
    voice_id = getattr(settings, setting.lower())
    if not voice_id:
        raise VoiceConfigError(f"{setting} is blank. Set it in .env to an ElevenLabs voice ID.")
    return voice_id


def check_voices(clinics: list[Clinic]) -> dict[str, str]:
    """Startup check: the API key and every needed voice ID are set. Raises one error naming every blank.

    Returns {"lana": voice_id, <clinic id>: voice_id, ...}.
    """
    problems = []
    if not get_settings().elevenlabs_api_key.get_secret_value():
        problems.append("ELEVENLABS_API_KEY is blank. Set it in .env.")
    voices = {}
    for name, speaker, clinic in [("lana", Speaker.AGENT, None)] + [(c.id, Speaker.CLINIC, c) for c in clinics]:
        try:
            voices[name] = voice_for(speaker, clinic)
        except VoiceConfigError as err:
            problems.append(f"{name}: {err}")
    if problems:
        raise VoiceConfigError("Text to speech is not set up:\n  " + "\n  ".join(problems))
    return voices


def voice_settings(stability: float | None = None) -> VoiceSettings:
    """Voice settings from .env; `stability` overrides ELEVENLABS_STABILITY for one line."""
    settings = get_settings()
    return VoiceSettings(
        stability=settings.elevenlabs_stability if stability is None else stability,
        similarity_boost=settings.elevenlabs_similarity,
        style=settings.elevenlabs_style,
        use_speaker_boost=settings.elevenlabs_speaker_boost,
    )


def model_for(speaker: Speaker) -> str:
    """ELEVENLABS_MODEL_AGENT for Lana, ELEVENLABS_MODEL_CLINIC for anyone else."""
    settings = get_settings()
    return settings.elevenlabs_model_agent if speaker == Speaker.AGENT else settings.elevenlabs_model_clinic


def _request(
    text: str, voice_id: str, speaker: Speaker, model: str | None, stability: float | None
) -> tuple[str, str, VoiceSettings, Path]:
    """The text to send, model, voice settings and cache file for one line.

    The model and Lana's tag end up in the sent text or the key, so both are part of the cache key.
    """
    model = model or model_for(speaker)
    if model in AUDIO_TAG_MODELS:
        text = text.strip()
        tag = get_settings().elevenlabs_tag_agent.strip()
        if speaker == Speaker.AGENT and tag and text:
            text = f"{tag} {text}"
    else:
        text = strip_audio_tags(text)
    vs = voice_settings(stability)
    parts = (voice_id, model, vs.model_dump_json(exclude_none=True), text)
    key = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return text, model, vs, CACHE_DIR / f"{key}.mp3"


def cache_path(
    text: str, voice_id: str, *, speaker: Speaker, model: str | None = None, stability: float | None = None
) -> Path:
    return _request(text, voice_id, speaker, model, stability)[3]


def is_cached(
    text: str, voice_id: str, *, speaker: Speaker, model: str | None = None, stability: float | None = None
) -> bool:
    return cache_path(text, voice_id, speaker=speaker, model=model, stability=stability).is_file()


# One SDK client per event loop: its httpx connections can't be shared across loops.
_clients: dict[int, AsyncElevenLabs] = {}


def _client() -> AsyncElevenLabs:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _clients:
        _clients.clear()
        _clients[loop_id] = AsyncElevenLabs(
            api_key=get_settings().elevenlabs_api_key.get_secret_value(), timeout=TIMEOUT_SECONDS
        )
    return _clients[loop_id]


async def stream_speech(
    text: str, voice_id: str, *, speaker: Speaker, model: str | None = None, stability: float | None = None
) -> AsyncIterator[bytes]:
    """Yield mp3 chunks for one line. From the cache if it has the line, else from ElevenLabs.

    The model comes from the speaker (model_for); `model` and `stability` override it and
    ELEVENLABS_STABILITY for this line. Audio tags like [laughs] are sent only to models that
    perform them; for other models they are stripped first. Lana's lines get ELEVENLABS_TAG_AGENT
    in front when her model performs tags. Show strip_audio_tags(text) on screen either way.

    On any failure or a stall longer than TIMEOUT_SECONDS it logs and stops. If that
    happens before the first chunk the caller gets no audio and shows the text only.
    Only a fully received line is written to the cache.
    """
    if not voice_id:
        return
    text, model, vs, path = _request(text, voice_id, speaker, model, stability)
    if not text:
        return
    if path.is_file():
        log.info("tts cache hit: %d chars, voice %s", len(text), voice_id)
        with path.open("rb") as f:
            while chunk := f.read(READ_CHUNK_BYTES):
                yield chunk
        return

    log.info(
        "tts cache miss: sending %d chars to ElevenLabs (%s, voice %s, stability %s)",
        len(text), model, voice_id, vs.stability,
    )
    started = time.perf_counter()
    parts: list[bytes] = []
    stream = None
    try:
        stream = _client().text_to_speech.stream(
            voice_id,
            text=text,
            model_id=model,
            voice_settings=vs,
            output_format=OUTPUT_FORMAT,
            request_options={"timeout_in_seconds": TIMEOUT_SECONDS, "max_retries": 0},
        )
        while True:
            try:
                chunk = await asyncio.wait_for(anext(stream), TIMEOUT_SECONDS)
            except StopAsyncIteration:
                break
            if chunk:
                if not parts:
                    first_chunk_s = time.perf_counter() - started
                parts.append(chunk)
                yield chunk
    except Exception as err:  # never raise into the call loop
        kind = "timed out" if isinstance(err, TimeoutError) else f"failed: {type(err).__name__}: {err}"
        log.warning("tts %s after %.1fs (%d chars, voice %s)", kind, time.perf_counter() - started, len(text), voice_id)
        return
    finally:
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:
                pass

    if not parts:
        log.warning("tts returned no audio (%d chars, voice %s)", len(text), voice_id)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(b"".join(parts))
        tmp.replace(path)
    except OSError as err:
        log.warning("tts cache write failed: %s", err)
    log.info(
        "tts done in %.1fs (first chunk %.2fs): %d chars, %d bytes",
        time.perf_counter() - started, first_chunk_s, len(text), sum(map(len, parts)),
    )


async def prewarm(lines: list[tuple[str, str]], speaker: Speaker = Speaker.AGENT) -> list[str]:
    """Generate and cache (text, voice_id) pairs for one speaker. Returns "cached", "generated" or "failed" per line."""
    results = []
    for text, voice_id in lines:
        if is_cached(text, voice_id, speaker=speaker):
            results.append("cached")
            continue
        async for _ in stream_speech(text, voice_id, speaker=speaker):
            pass
        results.append("generated" if is_cached(text, voice_id, speaker=speaker) else "failed")
    return results
