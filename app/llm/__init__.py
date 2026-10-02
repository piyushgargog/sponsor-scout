from .base import LLMError, LLMProvider, parse_json_loose, validate_schema  # noqa: F401


def get_llm(settings) -> LLMProvider:
    p = settings.llm_provider
    if p == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    if p == "openai":
        from .openai_stub import OpenAIProvider
        return OpenAIProvider()
    from .mock import MockProvider
    return MockProvider()
