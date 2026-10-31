"""tts.py with the ElevenLabs SDK mocked. Only the live test hits the network."""

import asyncio

import pytest

from app.config import Settings, get_settings
from app.data.loader import get_public_clinics
from app.schemas.enums import Speaker
from app.voice import tts
from app.voice.tts import VoiceConfigError, check_voices, prewarm, stream_speech, strip_audio_tags, voice_for


def _clinic(clinic_id: str):
    return next(c for c in get_public_clinics() if c.id == clinic_id)


async def _collect(text: str, voice_id: str, speaker: Speaker = Speaker.AGENT, **overrides) -> bytes:
    return b"".join([chunk async for chunk in stream_speech(text, voice_id, speaker=speaker, **overrides)])


def _path(text: str, voice_id: str, speaker: Speaker = Speaker.AGENT, **overrides):
    return tts.cache_path(text, voice_id, speaker=speaker, **overrides)


@pytest.fixture
def voices(monkeypatch):
    settings = get_settings()
    for name, value in {
        "voice_agent": "lana-voice",
        "voice_clinic_1": "clinic-1-voice",
        "voice_clinic_2": "clinic-2-voice",
        "voice_clinic_3": "clinic-3-voice",
        "elevenlabs_model_agent": "test-model",
        "elevenlabs_model_clinic": "clinic-model",
        "elevenlabs_tag_agent": "[warmly]",
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    return tmp_path


class FakeTTS:
    """Stands in for client.text_to_speech: streams scripted chunks, or fails, and records each request."""

    def __init__(self, chunks=(b"ab", b"cd"), error: Exception | None = None, delay: float = 0) -> None:
        self.chunks, self.error, self.delay = list(chunks), error, delay
        self.calls: list[dict] = []

    async def stream(self, voice_id, **kwargs):
        self.calls.append({"voice_id": voice_id, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def fake(monkeypatch):
    fake = FakeTTS()

    class FakeClient:
        text_to_speech = fake

    monkeypatch.setattr(tts, "_client", lambda: FakeClient())
    return fake


def test_lana_gets_voice_agent(voices):
    assert voice_for(Speaker.AGENT) == "lana-voice"


def test_each_clinic_gets_its_own_voice(voices):
    assert voice_for(Speaker.CLINIC, _clinic("parkway")) == "clinic-1-voice"
    assert voice_for(Speaker.CLINIC, _clinic("sunrise")) == "clinic-2-voice"
    assert voice_for(Speaker.CLINIC, _clinic("lakeside")) == "clinic-3-voice"


def test_blank_voice_id_gives_clear_error(voices, monkeypatch):
    monkeypatch.setattr(voices, "voice_clinic_2", "")
    with pytest.raises(VoiceConfigError, match="VOICE_CLINIC_2 is blank"):
        voice_for(Speaker.CLINIC, _clinic("sunrise"))


def test_startup_check_names_every_blank(voices, monkeypatch):
    monkeypatch.setattr(voices, "voice_agent", "")
    monkeypatch.setattr(voices, "voice_clinic_3", "")
    with pytest.raises(VoiceConfigError) as err:
        check_voices(get_public_clinics())
    assert "VOICE_AGENT is blank" in str(err.value)
    assert "VOICE_CLINIC_3 is blank" in str(err.value)


def test_clinic_voice_needs_a_clinic(voices):
    with pytest.raises(VoiceConfigError):
        voice_for(Speaker.CLINIC)


async def test_miss_streams_and_caches(voices, cache_dir, fake):
    assert await _collect("Hi there", "lana-voice") == b"abcd"
    assert fake.calls[0]["voice_id"] == "lana-voice"
    assert fake.calls[0]["model_id"] == "test-model"
    assert fake.calls[0]["output_format"].startswith("mp3")
    assert _path("Hi there", "lana-voice").read_bytes() == b"abcd"


async def test_cache_hit_makes_no_api_call(voices, cache_dir, fake):
    _path("Hi there", "lana-voice").write_bytes(b"cached audio")
    assert await _collect("Hi there", "lana-voice") == b"cached audio"
    assert fake.calls == []


def test_cache_key_depends_on_voice_model_and_tag(voices, cache_dir, monkeypatch):
    first = _path("Hi there", "lana-voice")
    assert _path("Hi there", "clinic-1-voice") != first
    assert _path("Hi there", "lana-voice", Speaker.CLINIC) != first  # clinic-model
    monkeypatch.setattr(voices, "elevenlabs_model_agent", "other-model")
    assert _path("Hi there", "lana-voice") != first
    tagged = _path("Hi there", "lana-voice", model="eleven_v4_turbo")
    monkeypatch.setattr(voices, "elevenlabs_tag_agent", "[cheerfully]")
    assert _path("Hi there", "lana-voice", model="eleven_v4_turbo") != tagged


async def test_failure_yields_nothing_and_does_not_raise(voices, cache_dir, fake):
    fake.error = RuntimeError("401 bad key")
    assert await _collect("Hi there", "lana-voice") == b""
    assert not _path("Hi there", "lana-voice").exists()


async def test_timeout_yields_nothing(voices, cache_dir, fake, monkeypatch):
    monkeypatch.setattr(tts, "TIMEOUT_SECONDS", 0.05)
    fake.delay = 1
    assert await _collect("Hi there", "lana-voice") == b""
    assert not _path("Hi there", "lana-voice").exists()


async def test_voice_settings_are_sent(voices, cache_dir, fake, monkeypatch):
    monkeypatch.setattr(voices, "elevenlabs_stability", 0.4)
    monkeypatch.setattr(voices, "elevenlabs_similarity", 0.8)
    monkeypatch.setattr(voices, "elevenlabs_style", 0.3)
    monkeypatch.setattr(voices, "elevenlabs_speaker_boost", True)
    await _collect("Hi there", "lana-voice")
    vs = fake.calls[0]["voice_settings"]
    assert (vs.stability, vs.similarity_boost, vs.style, vs.use_speaker_boost) == (0.4, 0.8, 0.3, True)


async def test_model_and_stability_overrides(voices, cache_dir, fake):
    assert await _collect("Hi there", "lana-voice", model="eleven_v4_turbo", stability=0.3) == b"abcd"
    assert fake.calls[0]["model_id"] == "eleven_v4_turbo"
    assert fake.calls[0]["voice_settings"].stability == 0.3


def test_cache_key_depends_on_voice_settings(voices, cache_dir, monkeypatch):
    first = _path("Hi there", "lana-voice")
    assert _path("Hi there", "lana-voice", stability=0.1) != first
    monkeypatch.setattr(voices, "elevenlabs_style", 0.9)
    assert _path("Hi there", "lana-voice") != first


@pytest.mark.parametrize(
    "text, shown",
    [
        ("[warmly] Hi, I'm Lana.", "Hi, I'm Lana."),
        ("Our total is $4,800 [pause], plus a $250 fee. [laughs]", "Our total is $4,800, plus a $250 fee."),
        ("No tags here.", "No tags here."),
        ("[sighs][curious] Really?", "Really?"),
    ],
)
def test_strip_audio_tags(text, shown):
    assert strip_audio_tags(text) == shown


async def test_audio_tags_sent_only_to_models_that_perform_them(voices, cache_dir, fake):
    await _collect("[curious] Really?", "clinic-1-voice", Speaker.CLINIC)  # clinic-model: no audio tags
    await _collect("[curious] Really?", "clinic-1-voice", Speaker.CLINIC, model="eleven_v4_turbo")
    assert [c["text"] for c in fake.calls] == ["Really?", "[curious] Really?"]


async def test_model_is_picked_by_speaker(voices, cache_dir, fake):
    await _collect("Hi there", "lana-voice")
    await _collect("Our total is $4,800.", "clinic-1-voice", Speaker.CLINIC)
    assert [c["model_id"] for c in fake.calls] == ["test-model", "clinic-model"]


async def test_lana_tag_only_when_her_model_supports_tags(voices, cache_dir, fake, monkeypatch):
    await _collect("Hi there", "lana-voice")  # test-model: no tags
    monkeypatch.setattr(voices, "elevenlabs_model_agent", "eleven_v4_turbo")
    await _collect("Hi there", "lana-voice")
    await _collect("Hi there", "clinic-1-voice", Speaker.CLINIC, model="eleven_v4_turbo")  # clinics never get it
    assert [c["text"] for c in fake.calls] == ["Hi there", "[warmly] Hi there", "Hi there"]
    assert strip_audio_tags("[warmly] Hi there") == "Hi there"


def test_model_settings_fall_back_to_elevenlabs_model():
    defaults = Settings(_env_file=None)
    assert (defaults.elevenlabs_model_agent, defaults.elevenlabs_model_clinic) == ("eleven_v4_turbo", "eleven_flash_v2_5")
    assert defaults.elevenlabs_tag_agent == "[warmly]"
    shared = Settings(_env_file=None, elevenlabs_model="eleven_v3")
    assert (shared.elevenlabs_model_agent, shared.elevenlabs_model_clinic) == ("eleven_v3", "eleven_v3")
    mixed = Settings(_env_file=None, elevenlabs_model="eleven_v3", elevenlabs_model_clinic="eleven_flash_v2_5")
    assert (mixed.elevenlabs_model_agent, mixed.elevenlabs_model_clinic) == ("eleven_v3", "eleven_flash_v2_5")


async def test_prewarm_skips_cached_lines(voices, cache_dir, fake):
    _path("One", "lana-voice").write_bytes(b"x")
    assert await prewarm([("One", "lana-voice"), ("Two", "lana-voice")]) == ["cached", "generated"]
    assert [c["text"] for c in fake.calls] == ["Two"]


@pytest.mark.live
async def test_live_three_words(cache_dir):
    voice_id = voice_for(Speaker.AGENT)
    audio = await _collect("Hello from Lana.", voice_id)
    assert len(audio) > 1000
