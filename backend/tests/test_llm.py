"""call_json with the OpenAI client mocked. No network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.integrations import meta_llm
from app.integrations.meta_llm import LLMJsonError, call_json


class Greeting(BaseModel):
    text: str
    words: int


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.fixture
def create(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=mock)))
    monkeypatch.setattr(meta_llm, "get_client", lambda: client)
    return mock


def _prompt(call) -> str:
    return call.kwargs["messages"][0]["content"]


async def test_valid_json_passes(create: AsyncMock) -> None:
    create.return_value = _response('{"text": "hi there", "words": 2}')

    result = await call_json("Greet me.", Greeting, model="fast")

    assert result == Greeting(text="hi there", words=2)
    assert create.await_count == 1
    assert '"words"' in _prompt(create.await_args_list[0])  # schema is in the prompt


async def test_invalid_then_valid_retries_once(create: AsyncMock) -> None:
    create.side_effect = [
        _response('{"text": "hi"}'),
        _response('{"text": "hi", "words": 1}'),
    ]

    result = await call_json("Greet me.", Greeting, model="fast")

    assert result == Greeting(text="hi", words=1)
    assert create.await_count == 2
    retry_prompt = _prompt(create.await_args_list[1])
    assert "Validation error" in retry_prompt
    assert "words" in retry_prompt.split("Validation error", 1)[1]


async def test_invalid_twice_raises(create: AsyncMock) -> None:
    create.side_effect = [_response("not json"), _response("still not json")]

    with pytest.raises(LLMJsonError) as exc_info:
        await call_json("Greet me.", Greeting, model="fast")

    assert exc_info.value.raw == "still not json"
    assert create.await_count == 2
