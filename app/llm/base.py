"""LLM provider abstraction. The app depends ONLY on generate() / generate_json()."""
import json
import re
from abc import ABC, abstractmethod


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    """The provider is not signed in / not configured. A human must fix this; retrying will not help."""


class LLMQuotaError(LLMError):
    """The account's usage quota or rate limit was hit. Never retried or bypassed; wait for the quota to reset."""


def parse_json_loose(text: str):
    """Parse JSON possibly wrapped in markdown fences or surrounded by prose."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    raise LLMError("model did not return valid JSON")


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def validate_schema(data, schema: dict, path="$") -> list[str]:
    """Tiny JSON-schema subset validator: type, required, properties, items."""
    errs = []
    t = schema.get("type")
    if t == "number":
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            return [f"{path}: expected number"]
    elif t == "integer":
        if isinstance(data, bool) or not isinstance(data, int):
            return [f"{path}: expected integer"]
    elif t in _TYPES and not isinstance(data, _TYPES[t]):
        return [f"{path}: expected {t}"]
    if t == "object":
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}.{k}: missing")
        for k, sub in schema.get("properties", {}).items():
            if k in data and data[k] is not None:
                errs += validate_schema(data[k], sub, f"{path}.{k}")
    if t == "array" and "items" in schema:
        for i, it in enumerate(data[:200]):
            errs += validate_schema(it, schema["items"], f"{path}[{i}]")
    return errs


def json_from_text(call, prompt: str, schema: dict | None, attempts: int = 2):
    """Shared JSON mode for text-only providers: ask for JSON, parse leniently, validate locally, and
    give the model one repair attempt. `call(prompt) -> str`. Raises LLMError('invalid JSON: ...')."""
    full = prompt
    if schema:
        full += "\n\nReturn ONLY JSON matching this schema (no prose, no markdown):\n" + json.dumps(schema)
    last_err = ""
    for _ in range(attempts):
        text = call(full)
        try:
            data = parse_json_loose(text)
            errs = validate_schema(data, schema) if schema else []
            if not errs:
                return data
            last_err = "; ".join(errs[:3])
        except LLMError as e:
            last_err = str(e)
        full += f"\n\nYour previous reply was invalid ({last_err}). Reply again with valid JSON only."
    raise LLMError(f"invalid JSON: {last_err}")


class LLMProvider(ABC):
    name = "base"
    model = ""

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.4,
                 max_tokens: int | None = None) -> str:
        ...

    @abstractmethod
    def generate_json(self, prompt: str, *, system: str | None = None, schema: dict | None = None,
                      task: str | None = None, context: dict | None = None, temperature: float = 0.2):
        """Return parsed JSON. `task`/`context` are hints used only by MockProvider; real providers
        rely on the prompt text, which already embeds all context."""
        ...
