"""Structured JSON logging with secret redaction. Never logs keys/tokens."""
import json
import logging
import re
import sys

_REDACT = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"), "[REDACTED_TOKEN]"),
    (re.compile(r"1//[0-9A-Za-z_\-]{20,}"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"(?i)bearer\s+[0-9A-Za-z_\-\.=]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|password|x-goog-api-key)(\"?\s*[:=]\s*\"?)[^\s\"&,}]+"),
     r"\1\2[REDACTED]"),
]
_SENSITIVE_KEYS = {"api_key", "key", "token", "access_token", "refresh_token", "client_secret", "password", "secret", "authorization"}


def redact(text: str) -> str:
    for rx, repl in _REDACT:
        text = rx.sub(repl, text)
    return text


def _clean(v):
    if isinstance(v, dict):
        return {k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else _clean(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, str):
        return redact(v)
    return v


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "event": getattr(record, "event", record.getMessage())}
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(_clean(extra))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


_logger = logging.getLogger("sponsor")


def setup_logging(level: str = "INFO") -> None:
    if _logger.handlers:
        _logger.setLevel(level)
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    _logger.addHandler(h)
    _logger.setLevel(level)
    _logger.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Events: research_started, research_completed, llm_request, llm_failure, lead_created,
    email_generated, email_approved, email_sent, email_failed, ..."""
    _logger.log(level, event, extra={"event": event, "fields": fields})
