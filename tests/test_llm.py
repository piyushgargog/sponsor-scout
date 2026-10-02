import json
import unittest
from unittest import mock

from app.config import Settings
from app.llm import get_llm
from app.llm.base import LLMError, parse_json_loose, validate_schema
from app.llm.gemini import GeminiProvider, rank_models
from app.llm.mock import MockProvider
from app.llm.openai_stub import OpenAIProvider
from tests.helpers import LogCapture

KEY = "AIzaSyFAKEKEYFAKEKEYFAKEKEYFAKEKEY12345"


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text or json.dumps(data or {})

    def json(self):
        return self._d


def gen(text):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


MODELS = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                     ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3-pro-image", "gemini-3.1-flash-tts-preview",
                      "text-embedding-004", "gemini-2.5-flash-lite", "gemini-live-2.5-flash", "gemma-3-27b"]]}


class RankingTests(unittest.TestCase):
    def test_prefers_stable_then_newest_and_skips_non_text(self):
        ranked = rank_models(MODELS["models"])
        self.assertEqual(ranked[0], "gemini-3.5-flash")
        self.assertNotIn("gemini-3-pro-image", ranked)
        self.assertNotIn("gemini-3.1-flash-tts-preview", ranked)
        self.assertNotIn("text-embedding-004", ranked)
        self.assertNotIn("gemini-live-2.5-flash", ranked)
        self.assertLess(ranked.index("gemini-2.5-pro"), ranked.index("gemini-2.5-flash"))
        self.assertLess(ranked.index("gemini-2.5-flash"), ranked.index("gemini-3.1-pro-preview"))  # stable before preview

    def test_falls_back_to_preview_when_nothing_stable(self):
        only = [{"name": "models/gemini-3.1-pro-preview"}, {"name": "models/gemini-3-flash-preview"}]
        self.assertEqual(rank_models(only)[0], "gemini-3.1-pro-preview")


class GeminiTests(unittest.TestCase):
    def provider(self, **kw):
        sess = mock.MagicMock()
        return GeminiProvider(KEY, session=sess, **kw), sess

    def test_requires_key(self):
        with self.assertRaises(LLMError):
            GeminiProvider("")

    def test_configured_model_is_used_without_discovery(self):
        p, s = self.provider(model="models/gemini-3.1-pro-preview")
        s.post.return_value = gen("hello")
        self.assertEqual(p.generate("hi"), "hello")
        s.get.assert_not_called()
        url = s.post.call_args[0][0]
        self.assertIn("/models/gemini-3.1-pro-preview:generateContent", url)
        self.assertEqual(s.post.call_args[1]["headers"]["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, url)  # key travels in a header, never in the URL

    def test_auto_discovery(self):
        p, s = self.provider()
        s.get.return_value = Resp(200, MODELS)
        s.post.return_value = gen("ok")
        p.generate("x")
        self.assertEqual(p.model, "gemini-3.5-flash")

    def test_discovery_failure_is_actionable(self):
        p, s = self.provider()
        s.get.return_value = Resp(403, text="forbidden")
        with self.assertRaises(LLMError) as cm:
            p.generate("x")
        self.assertIn("models.list", str(cm.exception))

    def test_json_mode_and_fence_stripping(self):
        p, s = self.provider(model="m")
        s.post.return_value = gen('```json\n{"a": 1}\n```')
        self.assertEqual(p.generate_json("x", schema={"type": "object", "required": ["a"]}), {"a": 1})
        body = s.post.call_args[1]["json"]
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_invalid_json_repaired_once_then_fails(self):
        p, s = self.provider(model="m")
        s.post.side_effect = [gen("not json"), gen('{"a": 2}')]
        self.assertEqual(p.generate_json("x"), {"a": 2})
        s.post.side_effect = [gen("nope"), gen("still nope")]
        with self.assertRaises(LLMError):
            p.generate_json("x")

    def test_schema_violation_is_retried(self):
        p, s = self.provider(model="m")
        s.post.side_effect = [gen('{"b": 1}'), gen('{"a": 1}')]
        self.assertEqual(p.generate_json("x", schema={"type": "object", "required": ["a"]}), {"a": 1})

    def test_retries_on_429_then_succeeds(self):
        p, s = self.provider(model="m")
        s.post.side_effect = [Resp(429, text="slow down"), Resp(503, text="x"), gen("fine")]
        with mock.patch("app.llm.gemini.time.sleep"):
            self.assertEqual(p.generate("x"), "fine")
        self.assertEqual(s.post.call_count, 3)

    def test_unknown_model_gives_helpful_error(self):
        p, s = self.provider(model="gemini-9-ultra")
        s.post.return_value = Resp(404, text="not found")
        with self.assertRaises(LLMError) as cm:
            p.generate("x")
        self.assertIn("python -m app.cli models", str(cm.exception))

    def test_empty_or_blocked_response(self):
        p, s = self.provider(model="m")
        s.post.return_value = Resp(200, {"candidates": [{"finishReason": "SAFETY"}]})
        with self.assertRaises(LLMError):
            p.generate("x")

    def test_api_key_never_logged(self):
        p, s = self.provider(model="m")
        s.post.return_value = Resp(500, text=f"error for key {KEY} Bearer abc.def")
        with LogCapture() as lc, mock.patch("app.llm.gemini.time.sleep"):
            with self.assertRaises(LLMError) as cm:
                p.generate("x")
        self.assertNotIn(KEY, lc.text)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("llm_failure", lc.text)


class MiscLLMTests(unittest.TestCase):
    def test_validate_schema(self):
        sch = {"type": "object", "required": ["x"], "properties": {"x": {"type": "array", "items": {"type": "string"}}, "n": {"type": "number"}}}
        self.assertEqual(validate_schema({"x": ["a"], "n": 1.5}, sch), [])
        self.assertTrue(validate_schema({"x": [1]}, sch))
        self.assertTrue(validate_schema({}, sch))
        self.assertTrue(validate_schema([], sch))

    def test_parse_json_loose(self):
        self.assertEqual(parse_json_loose('Sure! {"a": [1,2]} done'), {"a": [1, 2]})
        with self.assertRaises(LLMError):
            parse_json_loose("nothing")

    def test_factory(self):
        self.assertIsInstance(get_llm(Settings(llm_provider="mock")), MockProvider)
        self.assertEqual(get_llm(Settings(llm_provider="gemini", gemini_api_key=KEY, gemini_model="m")).name, "gemini")
        self.assertIsInstance(get_llm(Settings(llm_provider="openai")), OpenAIProvider)
        with self.assertRaises(LLMError):
            OpenAIProvider().generate("x")

    def test_provider_interface_is_what_the_app_uses(self):
        for p in (MockProvider(), GeminiProvider(KEY, model="m"), OpenAIProvider()):
            self.assertTrue(callable(p.generate) and callable(p.generate_json))


if __name__ == "__main__":
    unittest.main()
