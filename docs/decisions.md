# Decisions

Newest first.

## 2026-10-31: TTS model per speaker, and a reworded disclosure

- Lana uses `eleven_v4_turbo` (`ELEVENLABS_MODEL_AGENT`) with a `[warmly]` audio tag (`ELEVENLABS_TAG_AGENT`) in front of each line for a more natural, expressive voice. The tag is sent only when her model supports audio tags and is never shown on screen.
- Clinics use `eleven_flash_v2_5` (`ELEVENLABS_MODEL_CLINIC`) because it is cheaper and faster, and it sounded better for the clinic voices in a side-by-side test against `eleven_v4_turbo`.
- `ELEVENLABS_MODEL` still works as a fallback for either speaker when set. The model and the tag are part of the TTS cache key.
- The disclosure was reworded to sound natural: "Hi there! This is Lana, I'm an AI assistant calling for a patient. Just so you know, this call's recorded." It still states that Lana is an AI and that the call is recorded. The name comes from `AGENT_NAME`.

## 2026-10-31: eleven_v4_turbo, voice settings and audio tags

Sources: ElevenLabs Models, https://elevenlabs.io/docs/models; Stream speech API reference, https://elevenlabs.io/docs/api-reference/text-to-speech/stream; prompting guide (audio tags, covers Eleven v4), https://elevenlabs.io/docs/best-practices/prompting/eleven-v3; Text to Dialogue stream, https://elevenlabs.io/docs/api-reference/text-to-dialogue/stream; plus a live `GET /v1/models` with our key.

- Model ID is `eleven_v4_turbo` (released with `eleven_v4`). The Models page calls it the real-time model, with a median latency of about 100 ms not counting the network, and mentions only "the Text to Dialogue websocket" as where it's available. `eleven_turbo_v2_5` and `eleven_turbo_v2` are now deprecated.
- Streaming through the Python SDK: `GET /v1/models` reports `can_do_text_to_speech: true` for `eleven_v4_turbo` (10,000 characters per request). The same `client.text_to_speech.stream(voice_id, text=..., model_id="eleven_v4_turbo", voice_settings=..., output_format=...)` call we already use works: tested live, HTTP 200, mp3 streamed. No code path change was needed.
- Audio tags: v4 Turbo lists "Audio tags for fine-grained control". The syntax is inline square brackets, for example `[laughs]`, `[whispers]`, `[sighs]`, `[curious]`, `[excited]`. SSML `<break>` is not supported on v3 or v4 (third-party source, not confirmed in the official docs).
  - `tts.AUDIO_TAG_MODELS` = `eleven_v3`, `eleven_v3_conversational`, `eleven_v4`, `eleven_v4_turbo`. For any other model (for example flash), tags are stripped before sending so they are never read aloud.
  - On screen, always show `tts.strip_audio_tags(text)`.
- Voice settings (TTS API reference): `stability` (default 0.5), `similarity_boost` (0.75), `style` (0), `use_speaker_boost` (true), `speed` (1.0; the prompting guide gives 0.7 to 1.2). New settings `ELEVENLABS_STABILITY` 0.4, `ELEVENLABS_SIMILARITY` 0.8, `ELEVENLABS_STYLE` 0.3, `ELEVENLABS_SPEAKER_BOOST` true are sent with every line, and they are part of the cache key, so changing them makes new audio. Old cache files (keyed without settings) are simply never hit again.
- Caveat: `GET /v1/models` reports `can_use_style: false` and `can_use_speaker_boost: false` for `eleven_v4_turbo`, `eleven_v4`, `eleven_v3` and `eleven_flash_v2_5`, so `style` and speaker boost are probably ignored by these models. The API accepted them without an error. Stability 0.3 and 0.4 were accepted on v4 Turbo (no fixed 0 / 0.5 / 1 steps).
- `agent say` takes `--model` and `--stability` to compare without editing `.env`.
- First-chunk latency measured today (Lana disclosure, 83 characters, new connection each run): flash 0.58 s, v4 Turbo 0.69 s, v4 Turbo with stability 0.3 1.44 s. Parkway price line (45 characters): flash 0.81 s, v4 Turbo 0.82 s. One run each, so treat these as rough. Not decided yet which model to use; the default stays `eleven_flash_v2_5` until we listen.

## 2026-10-31: ElevenLabs streaming text to speech (voice/tts.py)

Sources: ElevenLabs API reference, Stream speech, https://elevenlabs.io/docs/api-reference/text-to-speech/stream; Get user subscription, https://elevenlabs.io/docs/api-reference/user/subscription/get; SDK README, https://github.com/elevenlabs/elevenlabs-python. The API reference shows only raw HTTP examples, so the SDK call below was confirmed in the installed SDK (`elevenlabs` 2.74.1, `text_to_speech/client.py`).

- Endpoint: `POST /v1/text-to-speech/{voice_id}/stream?output_format=...`, auth header `xi-api-key`.
- SDK call we use:
  ```python
  client = AsyncElevenLabs(api_key=..., timeout=8)
  async for chunk in client.text_to_speech.stream(
      voice_id, text=text, model_id="eleven_flash_v2_5", output_format="mp3_44100_128",
      request_options={"timeout_in_seconds": 8, "max_retries": 0},
  ):
      ...  # bytes
  ```
  On the async client, `stream` is an async generator: iterate it, don't `await` it.
- Model comes from the new setting `ELEVENLABS_MODEL` (default `eleven_flash_v2_5`). Output is `mp3_44100_128` (the API default; 192 kbps needs Creator tier).
- Timeout: 8 s for the request and 8 s for each wait on the next chunk. SDK retries are off. On any failure `stream_speech` logs and stops, so the caller shows the text only. Audio already yielded before a mid-line failure can't be taken back, and a partly received line is never cached.
- Cache: `backend/.cache/tts/<sha256(voice_id, model, text)>.mp3`, joined with newlines so the parts can't run together. Only fully received lines are written, through a temp file. `agent tts-prewarm` caches Lana's disclosure, greeting, the 6 intake questions and the hold request.
- Usage: `client.user.subscription.get()` returns `character_count` and `character_limit` (needs a key with User read access). Our key is Starter tier, 38,417 characters a month.
- Voice IDs: our voices come from the Voice Library. `voices.get(id)` returns 400 `voice_not_found` for a library voice until it has been used once, but text to speech still works with the ID. `agent tts-check` falls back to `voices.get_shared(search=id)` to confirm those.
- Measured latency (first chunk): about 5 to 6 s the first time a library voice is ever used, 0.75 s on a new connection afterwards, 0.13 s on a warm connection. So: one SDK client per event loop (connection reuse), and the server should prewarm or make one request per voice at startup.

## 2026-10-29: reasoning_effort="minimal" for live call turns (CALL_REASONING)

Source: Meta Model API docs, Reasoning guide, https://dev.meta.ai/docs/reasoning (re-checked today; matches the 2026-10-28 entry below).

- Chat Completions takes a top-level `reasoning_effort`. Reasoning cannot be turned off: `none` returns HTTP 400 on Muse Spark and Muse Wonder. The lowest level is `minimal`, "Shortest reasoning pass."
- `meta_llm.stream_chat` and `call_json` take an optional `reasoning` argument. When it is `None`, the parameter is left out and the model picks its own level, which is how every call worked before.
- New setting `CALL_REASONING` (default `minimal`) sets the level for live call turns. Demo receptionist turns (`clinics/simulator.py`) use it now.
- `meta_llm` now logs `usage.completion_tokens_details.reasoning_tokens`.
- Measured on `muse-spark-1.3` receptionist turns at `minimal`: 31 to 337 reasoning tokens per turn (most under 65), 0.9 to 3.1 s. A five-word ping without the setting used about 830 completion tokens. The price-match turn reasoned longest.

## 2026-10-28: Hidden reasoning tokens and the reasoning_effort setting

Observed: a five-word `agent ping` reply used about 850 completion tokens on `muse-spark-1.3` and 280 on `muse-wonder-1.0` (prompt was 14 tokens). Latency was 4.2 s and 3.4 s. This is likely hidden reasoning, which costs money and adds latency on every live call turn.

Source: Meta Model API docs, Reasoning guide, https://dev.meta.ai/docs/reasoning (also https://dev.meta.ai/docs/models).

- Chat Completions takes a top-level `reasoning_effort`. The Responses API nests it as `reasoning.effort`. The guide does not document one for the Messages API.
- Values: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.
- If the setting is left out, the model still reasons at a level it picks itself. That is what our calls do today.
- Reasoning cannot be turned off for our models: `none` is "Not supported by Muse Wonder or Muse Spark" and returns HTTP 400. The lowest level is `minimal`.
- `muse-spark-1.3` and `muse-wonder-1.0` both support `max`. `muse-spark-1.3-contributor` does not, which doesn't matter because we never use contributor IDs.
- Reasoning tokens show in `usage.completion_tokens_details.reasoning_tokens` (Chat Completions) and are billed at the output rate. They count toward `max_tokens`, so a low `max_tokens` can cut the visible reply short.
- Higher effort means more latency and cost. When streaming, the first visible chunk comes only after reasoning finishes. Raw reasoning is never streamed.
- Not decided yet; code unchanged. A likely next step: send `reasoning_effort="minimal"` (or `low`) for `MODEL_FAST` live call turns, leave it at the default or higher for `MODEL_SMART` work, and log `reasoning_tokens`.

## 2026-10-28: Meta Model API endpoint and model IDs confirmed

Source: Meta Model API docs, https://dev.meta.ai/docs/ (ai.developer.meta.com/docs redirects there).
Pages checked: https://dev.meta.ai/docs/models, https://dev.meta.ai/docs/pricing-rate-limits,
https://dev.meta.ai/docs/api-reference/chat-completions/create-chat-completion,
https://dev.meta.ai/docs/image-understanding.

- Base URL: `https://api.meta.ai/v1`. Auth is a Bearer key in `Authorization`.
- Chat Completions (`POST /chat/completions`) is the OpenAI-compatible endpoint, so we use the `openai` Python SDK with a custom `base_url`.
- Model IDs, matching `SETUP.md` section 2:
  - `MODEL_FAST` = `muse-spark-1.3` (Standard mode; `1.2` and `1.1` also exist)
  - `MODEL_SMART` = `muse-wonder-1.0` (preview, 1,048,576-token context)
  - `MODEL_STT` = `muse-voice-transcribe-1.0`, realtime at `wss://api.meta.ai/v1/asr/realtime` (file transcription at `POST /v1/asr/transcribe`)
- Standard mode: "your content is not used to train Meta models." Contributor mode is chosen only by the `-contributor` model ID suffix (for example `muse-spark-1.3-contributor`). There is no header or parameter for it. **Never put a `-contributor` ID in `.env`.** Wonder and Voice Transcribe have no Contributor mode.
- Muse Spark and Muse Wonder accept the same requests on the same endpoints, including image input.
- Images in Chat Completions go in a `user` message as `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}`. Allowed types: jpeg, png, gif, webp, x-icon. Up to 50 MB per inline image and 50 images per request.
- Not confirmed in the docs: whether `response_format` / JSON mode works on Chat Completions. So `meta_llm.py` puts the JSON schema in the prompt and validates with Pydantic.
- Token usage is returned when streaming (seen in a live `agent ping` without `stream_options`), and `meta_llm.py` logs it.
- Timeout is 30 s per request, and the SDK's own automatic retries are off (`max_retries=0`) so one call never takes longer than 30 s and the demo never stalls. The only retry is `call_json`'s single retry for invalid JSON.
