from .base import LLMError, LLMProvider


class OpenAIProvider(LLMProvider):
    """Intentional stub: shows where another provider plugs in. Implement generate/generate_json
    (JSON mode + parse_json_loose + validate_schema, as in GeminiProvider) to enable it."""
    name = "openai"

    def generate(self, *a, **k):
        raise LLMError("OpenAIProvider is a stub in this MVP. Implement app/llm/openai_stub.py or use LLM_PROVIDER=gemini.")

    def generate_json(self, *a, **k):
        raise LLMError("OpenAIProvider is a stub in this MVP.")
