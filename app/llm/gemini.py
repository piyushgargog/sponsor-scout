"""Gemini provider over the public REST API (generateContent). Model is configurable; if unset the
strongest *generally available* text model is discovered from models.list."""
import re
import time

import requests

from ..logging_setup import log_event, redact
from .base import LLMError, LLMProvider, parse_json_loose, validate_schema

BASE = "https://generativelanguage.googleapis.com/v1beta"
_EXCLUDE = re.compile(r"(image|tts|audio|live|embed|aqa|veo|imagen|lyria|robotics|computer-use|customtools|thinking-exp|vision|native|gemma|learnlm|deep-research|antigravity)", re.I)
_UNSTABLE = re.compile(r"(preview|exp|latest)", re.I)
_TIER = {"pro": 3, "flash": 2, "flash-lite": 1}


def rank_models(models: list[dict]) -> list[str]:
    """Order text-generation models best-first: stable before preview, then newest version, then pro>flash>lite."""
    scored = []
    for m in models:
        name = m.get("name", "").removeprefix("models/")
        if not name.startswith("gemini-") or _EXCLUDE.search(name):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or ["generateContent"]):
            continue
        v = re.match(r"gemini-(\d+(?:\.\d+)?)-(.+)", name)
        if not v:
            continue
        version = float(v.group(1))
        tier_key = v.group(2)
        tier = next((t for k, t in sorted(_TIER.items(), key=lambda kv: -len(kv[0])) if tier_key.startswith(k)), 0)
        stable = 0 if _UNSTABLE.search(name) else 1
        scored.append(((stable, version, tier), name))
    return [n for _, n in sorted(scored, reverse=True)]


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "", session=None, timeout: int = 90, retries: int = 3):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._key = api_key
        self.model = model.strip().removeprefix("models/") if model else ""
        self.http = session or requests.Session()
        self.timeout, self.retries = timeout, retries

    # ---- model selection -------------------------------------------------
    def list_models(self) -> list[dict]:
        out, token = [], None
        for _ in range(5):
            r = self.http.get(f"{BASE}/models", headers={"x-goog-api-key": self._key}, timeout=30,
                              params={"pageSize": 200, **({"pageToken": token} if token else {})})
            if r.status_code != 200:
                raise LLMError(f"models.list failed: HTTP {r.status_code} {redact(r.text[:200])}")
            data = r.json()
            out += data.get("models", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        ranked = rank_models(self.list_models())
        if not ranked:
            raise LLMError("Could not discover a Gemini model for this API key. Set GEMINI_MODEL explicitly.")
        self.model = ranked[0]
        log_event("llm_model_selected", provider="gemini", model=self.model, source="auto-discovery")
        return self.model

    # ---- transport -------------------------------------------------------
    def _call(self, body: dict) -> str:
        model = self.resolve_model()
        url = f"{BASE}/models/{model}:generateContent"
        last = ""
        t0 = time.time()
        for attempt in range(1, self.retries + 1):
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self._key}, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"network error: {type(e).__name__}"
            else:
                if r.status_code == 200:
                    data = r.json()
                    cands = data.get("candidates") or []
                    parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    if not text:
                        reason = (cands[0].get("finishReason") if cands else None) or (data.get("promptFeedback") or {}).get("blockReason")
                        raise LLMError(f"empty response (reason: {reason})")
                    log_event("llm_request", provider="gemini", model=model, ms=int((time.time() - t0) * 1000),
                              attempt=attempt, chars_out=len(text))
                    return text
                last = f"HTTP {r.status_code}: {redact(r.text[:300])}"
                if r.status_code == 404:
                    raise LLMError(f"Model '{model}' not found for this API key. Run `python -m app.cli models` to list "
                                   "available models and set GEMINI_MODEL.")
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(min(2 ** attempt, 8))
        log_event("llm_failure", provider="gemini", model=model, error=last)
        raise LLMError(f"Gemini request failed: {last}")

    def _body(self, prompt, system, temperature, max_tokens, json_mode):
        gc = {"temperature": temperature}
        if max_tokens:
            gc["maxOutputTokens"] = max_tokens
        if json_mode:
            gc["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    # ---- public API ------------------------------------------------------
    def generate(self, prompt, *, system=None, temperature=0.4, max_tokens=None) -> str:
        return self._call(self._body(prompt, system, temperature, max_tokens, False))

    def generate_json(self, prompt, *, system=None, schema=None, task=None, context=None, temperature=0.2):
        import json as _json
        full = prompt
        if schema:
            full += "\n\nReturn ONLY JSON matching this schema (no prose, no markdown):\n" + _json.dumps(schema)
        last_err = ""
        for attempt in range(2):
            text = self._call(self._body(full, system, temperature, None, True))
            try:
                data = parse_json_loose(text)
                errs = validate_schema(data, schema) if schema else []
                if not errs:
                    return data
                last_err = "; ".join(errs[:3])
            except LLMError as e:
                last_err = str(e)
            full += f"\n\nYour previous reply was invalid ({last_err}). Reply again with valid JSON only."
        log_event("llm_failure", provider="gemini", model=self.model, error=f"invalid JSON: {last_err}")
        raise LLMError(f"Gemini returned invalid JSON: {last_err}")
