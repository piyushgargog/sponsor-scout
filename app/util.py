import json
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ago(**kw) -> str:
    return (utcnow() - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_in(**kw) -> str:
    return (utcnow() + timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
