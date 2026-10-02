"""LLM provider abstraction. The app depends ONLY on generate() / generate_json()."""
import json
import re
from abc import ABC, abstractmethod


class LLMError(Exception):
    pass


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
