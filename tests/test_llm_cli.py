"""CliProvider (Google account route) tested against a fake CLI executable: no network, no Google account."""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from app.config import Settings
from app.llm import get_llm
from app.llm.base import LLMAuthError, LLMError, LLMQuotaError
from app.llm.cli import CliProvider, build_argv, detect_flavor, parse_envelope, safe_env

FAKE = '''#!{py}
import json, os, sys, time
log = os.environ.get("FAKE_CLI_LOG")
mode = os.environ.get("FAKE_CLI_MODE", "ok")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd()}}) + "\\n")
if "--version" in sys.argv:
    print("fake-cli 1.2.3"); sys.exit(0)
prompt = sys.argv[sys.argv.index("-p") + 1]
if mode == "ok":
    print(json.dumps({{"conversation_id": "c1", "status": "success", "response": "echo: " + prompt[-12:], "usage": {{}}}}))
elif mode == "json":
    print(json.dumps({{"status": "success", "response": "```json\\n{{\\"a\\": 1}}\\n```"}}))
elif mode == "badjson_then_ok":
    n = len(open(log).read().splitlines())  # noqa
    print(json.dumps({{"status": "success", "response": "not json" if n < 2 else "{{\\"a\\": 2}}"}}))
elif mode == "quota":
    print(json.dumps({{"status": "error", "error": {{"message": "RESOURCE_EXHAUSTED: quota exceeded for this account"}}}}))
elif mode == "auth":
    sys.stderr.write("Error: not signed in. Please log in."); sys.exit(1)
elif mode == "crash":
    sys.stderr.write("segfault-ish"); sys.exit(3)
elif mode == "sleep":
    time.sleep(30)
elif mode == "empty":
    print(json.dumps({{"status": "success", "response": ""}}))
elif mode == "text":
    print("plain text answer")
'''


class FakeCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "agy")
        with open(self.bin, "w") as f:
            f.write(FAKE.format(py=sys.executable))
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp.name, "calls.log")
        self.env = mock.patch.dict(os.environ, {"FAKE_CLI_LOG": self.log, "FAKE_CLI_MODE": "ok"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def provider(self, **kw):
        kw.setdefault("pass_env", ["FAKE_CLI_LOG", "FAKE_CLI_MODE"])
        kw.setdefault("retries", 0)
        return CliProvider(self.bin, **kw)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f.read().splitlines()]

    def mode(self, m):
        os.environ["FAKE_CLI_MODE"] = m


class ArgvTests(unittest.TestCase):
    def test_agy_argv_puts_prompt_last_after_p(self):
        a = build_argv("agy", "/x/agy", "hello", model="m1", timeout=60)
        self.assertEqual(a[-2:], ["-p", "hello"])
        self.assertEqual(a[a.index("--output-format") + 1], "json")
        self.assertEqual(a[a.index("--model") + 1], "m1")
        self.assertEqual(a[a.index("--print-timeout") + 1], "60s")

    def test_gemini_flavor_argv(self):
        a = build_argv("gemini", "gemini", "hi", model="gemini-x")
        self.assertEqual(a, ["gemini", "--output-format", "json", "-m", "gemini-x", "-p", "hi"])

    def test_never_skips_permissions(self):
        for fl in ("agy", "gemini"):
            self.assertNotIn("--dangerously-skip-permissions", build_argv(fl, "b", "p", "m", 5))
            self.assertNotIn("--yolo", build_argv(fl, "b", "p", "m", 5))

    def test_flavor_detection(self):
        self.assertEqual(detect_flavor("/usr/bin/gemini"), "gemini")
        self.assertEqual(detect_flavor("agy"), "agy")
        self.assertEqual(detect_flavor("/opt/Antigravity/bin/agy"), "agy")


class EnvelopeTests(unittest.TestCase):
    def test_parse_response_and_plain_text(self):
        self.assertEqual(parse_envelope(json.dumps({"status": "success", "response": "hi"})), "hi")
        self.assertEqual(parse_envelope("just text"), "just text")

    def test_error_and_empty(self):
        with self.assertRaises(LLMError):
            parse_envelope(json.dumps({"status": "error", "error": "boom"}))
        with self.assertRaises(LLMError):
            parse_envelope(json.dumps({"status": "success", "response": ""}))
        with self.assertRaises(LLMError):
            parse_envelope("")

    def test_error_text_is_redacted(self):
        with self.assertRaises(LLMError) as cm:
            parse_envelope(json.dumps({"error": "bad key AIzaSyFAKEKEYFAKEKEYFAKEKEYFAKEKEY12345"}))
        self.assertNotIn("AIzaSy", str(cm.exception))


class ProviderTests(FakeCliCase):
    def test_generate_runs_binary_headless_in_empty_tempdir(self):
        out = self.provider(model="some-model").generate("Say hi", system="Be terse")
        self.assertTrue(out.startswith("echo: "))
        call = self.calls()[0]
        self.assertEqual(call["argv"][-2], "-p")
        self.assertIn("Say hi", call["argv"][-1])
        self.assertIn("Be terse", call["argv"][-1])
        self.assertIn("Do NOT use any tools", call["argv"][-1])
        self.assertNotIn("--dangerously-skip-permissions", call["argv"])
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(os.getcwd()))

    def test_child_does_not_inherit_app_secrets(self):
        leaked = {"SECRET_KEY": "x" * 40, "GOOGLE_CLIENT_SECRET": "cs", "GEMINI_API_KEY": "k", "ADMIN_PASSWORD": "pw", "BRAVE_API_KEY": "b"}
        with mock.patch.dict(os.environ, leaked):
            self.provider().generate("hi")
        env = self.calls()[0]["env"]
        for k in leaked:
            self.assertNotIn(k, env)
        self.assertIn("PATH", env)
        self.assertIn("FAKE_CLI_MODE", env)   # explicitly passed via pass_env

    def test_safe_env_allowlist(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "s", "LC_ALL": "C", "XDG_RUNTIME_DIR": "/run/u", "EXTRA_OK": "1"}):
            e = safe_env(["EXTRA_OK"])
        self.assertNotIn("SECRET_KEY", e)
        self.assertIn("LC_ALL", e)
        self.assertIn("XDG_RUNTIME_DIR", e)
        self.assertEqual(e["EXTRA_OK"], "1")

    def test_generate_json_strips_markdown_fences(self):
        self.mode("json")
        self.assertEqual(self.provider().generate_json("p", schema={"type": "object", "required": ["a"]}), {"a": 1})

    def test_generate_json_repairs_once(self):
        self.mode("badjson_then_ok")
        self.assertEqual(self.provider().generate_json("p", schema={"type": "object", "required": ["a"]}), {"a": 2})
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("previous reply was invalid", self.calls()[1]["argv"][-1])

    def test_generate_json_gives_up_with_clear_error(self):
        with self.assertRaises(LLMError) as cm:
            self.provider().generate_json("p", schema={"type": "object", "required": ["zzz"]})   # fake returns plain 'echo: ...'
        self.assertIn("invalid JSON", str(cm.exception))

    def test_quota_is_classified_and_not_retried(self):
        self.mode("quota")
        with self.assertRaises(LLMQuotaError):
            self.provider(retries=2).generate("p")
        self.assertEqual(len(self.calls()), 1)

    def test_auth_failure_is_classified_and_not_retried(self):
        self.mode("auth")
        with self.assertRaises(LLMAuthError) as cm:
            self.provider(retries=2).generate("p")
        self.assertIn("sign in", str(cm.exception).lower())
        self.assertEqual(len(self.calls()), 1)

    def test_generic_failure_retries_then_raises(self):
        self.mode("crash")
        with mock.patch("app.llm.cli.time.sleep"):
            with self.assertRaises(LLMError) as cm:
                self.provider(retries=1).generate("p")
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIsInstance(cm.exception, (LLMAuthError, LLMQuotaError))

    def test_empty_response_is_an_error(self):
        self.mode("empty")
        with self.assertRaises(LLMError):
            self.provider().generate("p")

    def test_plain_text_stdout_accepted(self):
        self.mode("text")
        self.assertEqual(self.provider().generate("p"), "plain text answer")

    def test_timeout_kills_child_and_raises(self):
        self.mode("sleep")
        p = self.provider(timeout=1)
        p.grace = 0
        with self.assertRaises(LLMError) as cm:
            p.generate("p")
        self.assertIn("timed out", str(cm.exception))

    def test_missing_binary_gives_actionable_error(self):
        with self.assertRaises(LLMAuthError) as cm:
            CliProvider("definitely-not-installed-cli-xyz").generate("p")
        self.assertIn("not found", str(cm.exception))

    def test_oversized_prompt_rejected_before_spawning(self):
        with self.assertRaises(LLMError):
            self.provider().generate("x" * 200_000)
        self.assertEqual(self.calls(), [])

    def test_version(self):
        self.assertEqual(self.provider().version(), "fake-cli 1.2.3")

    def test_prompts_are_not_logged(self):
        from tests.helpers import LogCapture
        with LogCapture() as cap:
            self.provider().generate("TOP-SECRET-PROMPT-TEXT")
        self.assertIn("llm_request", cap.text)
        self.assertNotIn("TOP-SECRET-PROMPT-TEXT", cap.text)


class ConfigTests(FakeCliCase):
    def settings(self, **kw):
        return Settings(app_env="development", secret_key="s" * 40, admin_password="pw", **kw)

    def test_factory_builds_cli_provider_without_any_api_key(self):
        s = self.settings(llm_provider="cli", llm_cli_bin=self.bin, llm_cli_model="m")
        s.validate_for_runtime()          # no GEMINI_API_KEY required
        llm = get_llm(s)
        self.assertIsInstance(llm, CliProvider)
        self.assertEqual(llm.model, "m")
        self.assertEqual(llm.flavor, "agy")

    def test_cli_provider_requires_binary(self):
        with self.assertRaises(RuntimeError) as cm:
            self.settings(llm_provider="cli", llm_cli_bin="no-such-binary-xyz").validate_for_runtime()
        self.assertIn("PATH", str(cm.exception))

    def test_gemini_api_provider_still_requires_key_and_points_to_cli(self):
        with self.assertRaises(RuntimeError) as cm:
            self.settings(llm_provider="gemini").validate_for_runtime()
        self.assertIn("LLM_PROVIDER=cli", str(cm.exception))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(RuntimeError):
            self.settings(llm_provider="bogus").validate_for_runtime()

    def test_bad_flavor_rejected(self):
        with self.assertRaises(RuntimeError):
            self.settings(llm_provider="cli", llm_cli_bin=self.bin, llm_cli_flavor="nope").validate_for_runtime()

    def test_env_aliases_and_defaults(self):
        base = {k: "" for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "LLM_CLI_BIN", "LLM_CLI_FLAVOR", "LLM_FIT_ANALYSIS", "LLM_EMAIL_REVIEW", "LLM_CLI_CONCURRENCY")}
        with mock.patch.dict(os.environ, base), mock.patch("app.config.load_dotenv"):
            self.assertEqual(Settings.from_env().llm_provider, "mock")                 # offline by default
            self.assertTrue(Settings.from_env().llm_fit_analysis)
            for alias in ("antigravity", "gemini_cli", "agy", "cli"):
                with mock.patch.dict(os.environ, {"LLM_PROVIDER": alias}):
                    self.assertEqual(Settings.from_env().llm_provider, "cli")
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                self.assertEqual(Settings.from_env().llm_provider, "gemini")           # legacy key-only behaviour
            with mock.patch.dict(os.environ, {"LLM_FIT_ANALYSIS": "0", "LLM_EMAIL_REVIEW": "false", "LLM_CLI_CONCURRENCY": "0"}):
                s = Settings.from_env()
                self.assertFalse(s.llm_fit_analysis)
                self.assertFalse(s.llm_email_review)
                self.assertEqual(s.llm_cli_concurrency, 1)


if __name__ == "__main__":
    unittest.main()
