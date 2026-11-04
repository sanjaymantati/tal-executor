from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI, BadRequestError


@dataclass(frozen=True)
class LlmResponse:
    text: str
    total_tokens: int | None


class LlmClient(Protocol):
    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse: ...


class OpenAiLlmClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        proxy: str | None = None,
        prefer_json_response_format: bool = True,
    ) -> None:
        http_client = httpx.AsyncClient(proxy=proxy) if proxy else None
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        print(client_kwargs)
        self._client = AsyncOpenAI(**client_kwargs)
        self._model = model
        self._prefer_json_response_format = prefer_json_response_format

    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        completion = await self._create_completion(messages)
        usage = completion.usage
        return LlmResponse(
            text=completion.choices[0].message.content or "",
            total_tokens=usage.total_tokens if usage else None,
        )

    async def _create_completion(self, messages: list[dict[str, str]]):
        if self._prefer_json_response_format:
            try:
                return await self._request(messages, use_json_response_format=True)
            except BadRequestError as error:
                if not _json_response_format_rejected(error):
                    raise
        return await self._request(messages, use_json_response_format=False)

    async def _request(self, messages: list[dict[str, str]], *, use_json_response_format: bool):
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if use_json_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        return await self._client.chat.completions.create(**kwargs)


def _json_response_format_rejected(error: Exception) -> bool:
    message = str(error).lower()
    return "response_format" in message or "json_object" in message
