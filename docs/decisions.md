# Decisions

Newest first.

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
