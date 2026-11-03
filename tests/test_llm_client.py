import pytest

from app.config import DEFAULT_OLLAMA_BASE_URL, Settings
from app.enums import LlmProvider
from app.llm.client import _json_response_format_rejected
from app.llm.factory import build_llm_client


def minimal_settings(**overrides) -> Settings:
    base = {
        "tg_api_id": 1,
        "tg_api_hash": "hash",
        "tg_phone": "+10000000000",
        "tg_channel": "@chan",
        "openai_model": "test-model",
        "upstox_access_token": "token",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def test_openai_requires_api_key():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        minimal_settings(llm_provider=LlmProvider.OPENAI, openai_api_key="")


def test_ollama_applies_defaults():
    settings = minimal_settings(llm_provider=LlmProvider.OLLAMA)
    assert settings.openai_api_key == "ollama"
    assert settings.openai_base_url == DEFAULT_OLLAMA_BASE_URL


def test_ollama_respects_explicit_base_url_and_key():
    settings = minimal_settings(
        llm_provider=LlmProvider.OLLAMA,
        openai_api_key="custom",
        openai_base_url="http://host:11434/v1",
    )
    assert settings.openai_api_key == "custom"
    assert settings.openai_base_url == "http://host:11434/v1"


def test_build_llm_client_ollama_skips_json_response_format():
    settings = minimal_settings(llm_provider=LlmProvider.OLLAMA)
    client = build_llm_client(settings)
    assert client._prefer_json_response_format is False


def test_build_llm_client_openai_prefers_json_response_format():
    settings = minimal_settings(openai_api_key="sk-test")
    client = build_llm_client(settings)
    assert client._prefer_json_response_format is True


def test_json_response_format_rejection_detection():
    assert _json_response_format_rejected(RuntimeError("response_format json_object not supported"))
    assert not _json_response_format_rejected(RuntimeError("model not found"))
