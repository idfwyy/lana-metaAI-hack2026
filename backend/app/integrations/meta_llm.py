"""call_json() and stream_chat() over the Meta Model API (OpenAI-compatible Chat Completions).

Every AI call in the app goes through this module. Model IDs and the endpoint come from settings;
see docs/decisions.md for what the Meta docs confirm.
"""

import asyncio
import base64
import json
import logging
import time
import weakref
from collections.abc import AsyncIterator

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import get_settings

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0

# Wraps the caller's prompt; the task itself lives in app/prompts/*.md.
JSON_INSTRUCTIONS = (
    "\n\nReply with JSON only: one object that matches this JSON schema. "
    "No prose, no markdown, no code fences.\n\nJSON schema:\n{schema}"
)
RETRY_INSTRUCTIONS = (
    "\n\nYour previous reply was not valid for the schema. Validation error:\n{error}\n"
    "Reply again with corrected JSON only."
)


class LLMJsonError(Exception):
    """The model did not return valid JSON for the schema after one retry."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


# One client per event loop: the client's HTTP pool is bound to the loop that created it,
# so reusing it after asyncio.run() closes that loop fails with "Event loop is closed".
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncOpenAI]" = (
    weakref.WeakKeyDictionary()
)


def get_client() -> AsyncOpenAI:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = get_settings()
        api_key = settings.meta_api_key.get_secret_value()
        if not settings.meta_base_url or not api_key:
            raise RuntimeError("META_BASE_URL and META_API_KEY must be set in .env")
        # max_retries=0 keeps every request inside the 30 s budget; call_json does its own retry.
        client = AsyncOpenAI(
            base_url=settings.meta_base_url, api_key=api_key, timeout=TIMEOUT_S, max_retries=0
        )
        _clients[loop] = client
    return client


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image type; use PNG, JPEG, GIF or WebP")


def _user_message(text: str, images: list[bytes] | None) -> dict:
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "text", "text": text}]
    for data in images:
        url = f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode('ascii')}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts}


def _strip_fences(text: str) -> str:
    """Drop a ```json ... ``` wrapper if the model added one anyway."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _reasoning_kwargs(reasoning: str | None) -> dict:
    """`reasoning_effort` only when set; left out, the model picks its own level.

    Muse Spark and Muse Wonder reject "none"; "minimal" is the lowest (see docs/decisions.md).
    """
    return {"reasoning_effort": reasoning} if reasoning else {}


def _log_call(model: str, started: float, usage: object | None) -> None:
    latency_ms = (time.perf_counter() - started) * 1000
    if usage is not None:
        details = getattr(usage, "completion_tokens_details", None)
        log.info(
            "meta_llm model=%s latency_ms=%.0f prompt_tokens=%s completion_tokens=%s "
            "reasoning_tokens=%s",
            model,
            latency_ms,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(details, "reasoning_tokens", None),
        )
    else:
        log.info("meta_llm model=%s latency_ms=%.0f usage=not_returned", model, latency_ms)


async def _complete(
    prompt: str, model: str, images: list[bytes] | None, reasoning: str | None
) -> str:
    started = time.perf_counter()
    response = await get_client().chat.completions.create(
        model=model, messages=[_user_message(prompt, images)], **_reasoning_kwargs(reasoning)
    )
    _log_call(model, started, response.usage)
    return response.choices[0].message.content or ""


async def call_json[T: BaseModel](
    prompt: str,
    schema: type[T],
    model: str,
    images: list[bytes] | None = None,
    reasoning: str | None = None,
) -> T:
    """Ask for JSON matching `schema` and return it validated. Retries once with the error."""
    if not model:
        raise ValueError("model is empty; set MODEL_FAST / MODEL_SMART in .env")
    full_prompt = prompt + JSON_INSTRUCTIONS.format(
        schema=json.dumps(schema.model_json_schema(), indent=2)
    )

    raw = await _complete(full_prompt, model, images, reasoning)
    try:
        return schema.model_validate_json(_strip_fences(raw))
    except ValidationError as err:
        log.info("meta_llm model=%s invalid JSON, retrying once", model)
        retry_prompt = full_prompt + RETRY_INSTRUCTIONS.format(error=err)

    raw = await _complete(retry_prompt, model, images, reasoning)
    try:
        return schema.model_validate_json(_strip_fences(raw))
    except ValidationError as err:
        raise LLMJsonError(
            f"{model} returned invalid JSON for {schema.__name__} twice: {err}", raw
        ) from err


async def stream_chat(
    messages: list[dict], model: str, reasoning: str | None = None
) -> AsyncIterator[str]:
    """Yield text chunks as they arrive, so text-to-speech can start early."""
    if not model:
        raise ValueError("model is empty; set MODEL_FAST / MODEL_SMART in .env")
    started = time.perf_counter()
    usage = None
    stream = await get_client().chat.completions.create(
        model=model, messages=messages, stream=True, **_reasoning_kwargs(reasoning)
    )
    async for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
    _log_call(model, started, usage)
