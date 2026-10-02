"""LLM provider that drives a locally installed, user-authenticated Google agent CLI in headless mode.

Why: a Google AI Pro subscription is not an API key. Google's supported way to use the subscription from a
terminal tool is to sign in to the official CLI yourself (Sign in with Google). This provider never touches
those credentials: it only launches the official binary as a child process and reads its stdout.

Supported flavors (auto-detected from the binary name, override with LLM_CLI_FLAVOR):
  agy     Antigravity CLI, Google's successor to Gemini CLI for AI Pro/Ultra individuals.
          argv: agy --output-format json [--print-timeout Ns] [--model M] -p <prompt>      (-p must come last)
  gemini  Legacy Gemini CLI. Google stopped serving AI Pro/Ultra individual accounts through it on 2026-06-18;
          it still works with an API key / Vertex / Gemini Code Assist Standard or Enterprise.
          argv: gemini --output-format json [-m M] -p <prompt>

Safety properties (each covered by tests/test_llm_cli.py):
  * never passes --dangerously-skip-permissions; the agent is told not to use tools and runs in an empty temp dir
  * the child gets an allow-listed environment: app secrets (SECRET_KEY, GOOGLE_CLIENT_SECRET, GEMINI_API_KEY, ...)
    are NOT inherited
  * quota / auth errors are surfaced as LLMQuotaError / LLMAuthError and are never retried or worked around
  * prompts and credentials are never logged
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from ..logging_setup import log_event, redact
from .base import LLMAuthError, LLMError, LLMProvider, LLMQuotaError, json_from_text

FLAVORS = ("agy", "gemini")
# Variables a CLI legitimately needs (home dir for its credential store, keyring/DBus, proxies, locale, TLS bundles).
_ENV_ALLOW = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ", "DBUS_SESSION_BUS_ADDRESS",
              "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY",
              "NO_PROXY", "ALL_PROXY", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP"}
_ENV_ALLOW_PREFIX = ("LC_", "XDG_")
_QUOTA_RX = re.compile(r"quota|rate.?limit|resource.?exhausted|usage limit|too many requests|\b429\b", re.I)
_AUTH_RX = re.compile(r"not (?:logged|signed) in|log ?in required|sign[- ]?in required|unauthenticated|unauthorized|"
                      r"no active session|please (?:log|sign) ?in|re-?authenticate|authentication (?:failed|required)|"
                      r"no longer supported for gemini code assist", re.I)
_FAILED_STATUS = {"error", "failed", "failure", "cancelled", "canceled", "timeout", "timed_out"}
MAX_PROMPT_BYTES = 100_000   # prompt is passed as one argv element (Linux limit is ~128 KB per argument)

TOOL_FREE_PREAMBLE = (
    "You are being called as a plain text-generation function inside another application. Do NOT use any tools: "
    "do not read or write files, run commands, browse the web or call external services. Answer directly using only "
    "the material in this message, and output only what is asked for.\n\n")


def detect_flavor(binary: str) -> str:
    return "gemini" if os.path.basename(binary).lower().startswith("gemini") else "agy"


def safe_env(extra_names=()) -> dict:
    """Allow-listed copy of os.environ for the child process."""
    keep = _ENV_ALLOW | {n.strip() for n in extra_names if n.strip()}
    return {k: v for k, v in os.environ.items() if k in keep or k.startswith(_ENV_ALLOW_PREFIX)}


def build_argv(flavor: str, binary: str, prompt: str, model: str = "", timeout: int = 0) -> list[str]:
    if flavor == "gemini":
        argv = [binary, "--output-format", "json"]
        if model:
            argv += ["-m", model]
        return argv + ["-p", prompt]
    argv = [binary, "--output-format", "json"]
    if timeout:
        argv += ["--print-timeout", f"{int(timeout)}s"]
    if model:
        argv += ["--model", model]
    return argv + ["-p", prompt]          # the prompt must directly follow -p, and -p goes last


def parse_envelope(stdout: str) -> str:
    """Extract the model text from the CLI's JSON envelope ({"response": ..., "error": ..., "status": ...}).
    Plain-text stdout is accepted as-is (some versions/flags print text)."""
    out = (stdout or "").strip()
    if not out:
        raise LLMError("CLI produced no output")
    try:
        env = json.loads(out)
    except ValueError:
        return out
    if not isinstance(env, dict):
        return out
    err = env.get("error")
    status = str(env.get("status") or "").lower()
    if err or status in _FAILED_STATUS:
        msg = redact(json.dumps(err) if not isinstance(err, str) else err)[:300] if err else f"status={status}"
        _raise_classified(msg)
    resp = env.get("response")
    if not isinstance(resp, str) or not resp.strip():
        raise LLMError("CLI returned an empty response")
    return resp


def _raise_classified(text: str):
    if _QUOTA_RX.search(text):
        raise LLMQuotaError("The Google account's usage quota/rate limit was reached. Wait for it to reset; the app does "
                            f"not retry or bypass quotas. ({redact(text)[:160]})")
    if _AUTH_RX.search(text):
        raise LLMAuthError("The CLI is not signed in (or this client is no longer supported for your account). Run it "
                           f"interactively once and sign in with Google. ({redact(text)[:160]})")
    raise LLMError(f"CLI error: {redact(text)[:300]}")


class CliProvider(LLMProvider):
    def __init__(self, binary: str = "agy", model: str = "", flavor: str = "", timeout: int = 180, concurrency: int = 1,
                 pass_env=(), retries: int = 1):
        self.binary = binary or "agy"
        self.flavor = (flavor or detect_flavor(self.binary)).lower()
        if self.flavor not in FLAVORS:
            raise LLMError(f"LLM_CLI_FLAVOR must be one of {FLAVORS}")
        self.name = {"agy": "antigravity-cli", "gemini": "gemini-cli"}[self.flavor]
        self.model = (model or "").strip()
        self.timeout, self.retries = int(timeout), retries
        self.grace = 15                 # extra seconds beyond the CLI's own --print-timeout before we kill it
        self.pass_env = tuple(pass_env)
        self._slots = threading.BoundedSemaphore(max(1, int(concurrency)))

    # ---- process handling ------------------------------------------------
    def resolve_binary(self) -> str:
        path = shutil.which(self.binary)
        if not path:
            hint = ("Install Antigravity CLI from Google, run `agy` once and choose Sign in with Google."
                    if self.flavor == "agy" else "Install Gemini CLI and sign in (note: it no longer serves Google AI Pro individuals).")
            raise LLMAuthError(f"CLI binary '{self.binary}' was not found on PATH. {hint}")
        return path

    def _run(self, argv: list[str], timeout: int) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory(prefix="llm-cli-") as cwd:
            proc = subprocess.Popen(argv, cwd=cwd, env=safe_env(self.pass_env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                    start_new_session=(os.name != "nt"))
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.communicate()
                raise LLMError(f"CLI timed out after {timeout}s")
            return proc.returncode, out, err

    @staticmethod
    def _kill(proc):
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    def _call(self, prompt: str, system: str | None = None) -> str:
        text = TOOL_FREE_PREAMBLE + (f"SYSTEM INSTRUCTIONS:\n{system}\n\nTASK:\n" if system else "") + prompt
        if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise LLMError("Prompt is too large to pass to the CLI as a command-line argument.")
        binary = self.resolve_binary()
        argv = build_argv(self.flavor, binary, text, self.model, self.timeout)
        last = ""
        t0 = time.time()
        with self._slots:
            for attempt in range(1, self.retries + 2):
                try:
                    code, out, err = self._run(argv, self.timeout + self.grace)
                except LLMError as e:
                    last = str(e)
                else:
                    if code == 0:
                        try:
                            resp = parse_envelope(out)
                        except (LLMQuotaError, LLMAuthError):
                            raise
                        except LLMError as e:
                            last = str(e)
                        else:
                            log_event("llm_request", provider=self.name, model=self.model or "account-default",
                                      ms=int((time.time() - t0) * 1000), attempt=attempt, chars_out=len(resp))
                            return resp
                    else:
                        blob = f"{err}\n{out}"
                        if _QUOTA_RX.search(blob) or _AUTH_RX.search(blob):
                            _raise_classified(blob)
                        last = f"exit code {code}: {redact((err or out).strip()[-300:])}"
                if attempt <= self.retries:
                    time.sleep(2 * attempt)
        log_event("llm_failure", provider=self.name, error=last)
        raise LLMError(f"{self.name} failed: {last}")

    # ---- diagnostics (used by `python -m app.cli llm-check`) ------------------
    def version(self) -> str:
        code, out, err = self._run([self.resolve_binary(), "--version"], 30)
        if code != 0:
            raise LLMError(f"`{self.binary} --version` failed: {redact((err or out).strip()[-200:])}")
        return out.strip()

    def ping(self) -> str:
        return self.generate("Reply with exactly the single word: OK", temperature=0).strip()

    # ---- public API ------------------------------------------------------
    def generate(self, prompt, *, system=None, temperature=0.4, max_tokens=None) -> str:
        # The CLIs expose no temperature/max-token flags; the arguments exist to satisfy the shared interface.
        return self._call(prompt, system)

    def generate_json(self, prompt, *, system=None, schema=None, task=None, context=None, temperature=0.2):
        try:
            return json_from_text(lambda full: self._call(full, system), prompt, schema)
        except (LLMQuotaError, LLMAuthError):
            raise
        except LLMError as e:
            if str(e).startswith("invalid JSON"):
                log_event("llm_failure", provider=self.name, error=str(e))
                raise LLMError(f"{self.name} returned {e}") from e
            raise
