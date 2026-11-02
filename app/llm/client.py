from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI


@dataclass(frozen=True)
class LlmResponse:
    text: str
    total_tokens: int | None


class LlmClient(Protocol):
    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse: ...


class OpenAiLlmClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        usage = completion.usage
        return LlmResponse(
            text=completion.choices[0].message.content or "",
            total_tokens=usage.total_tokens if usage else None,
        )
