from app.config import Settings
from app.enums import LlmProvider
from app.llm.client import OpenAiLlmClient


def build_llm_client(settings: Settings) -> OpenAiLlmClient:
    base_url = settings.openai_base_url.strip() or None
    return OpenAiLlmClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=base_url,
        proxy=settings.openai_proxy.strip() or None,
        prefer_json_response_format=settings.llm_provider != LlmProvider.OLLAMA,
    )
