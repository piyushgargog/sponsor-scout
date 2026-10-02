from .base import LLMAuthError, LLMError, LLMProvider, LLMQuotaError, parse_json_loose, validate_schema  # noqa: F401


def get_llm(settings) -> LLMProvider:
    """The only place that maps configuration to an LLMProvider implementation."""
    p = settings.llm_provider
    if p == "cli":
        from .cli import CliProvider
        return CliProvider(settings.llm_cli_bin, settings.llm_cli_model, settings.llm_cli_flavor,
                           settings.llm_cli_timeout_seconds, settings.llm_cli_concurrency,
                           [n for n in settings.llm_cli_pass_env.split(",")])
    if p == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    if p == "openai":
        from .openai_stub import OpenAIProvider
        return OpenAIProvider()
    from .mock import MockProvider
    return MockProvider()
