"""Input validation for user-supplied forms."""
import re
from datetime import date


class ValidationError(Exception):
    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(v, maxlen=200) -> str:
    v = _CTRL.sub("", (v or "")).strip()
    return v[:maxlen]


def split_list(v, max_items=15, item_len=80) -> list[str]:
    if isinstance(v, list):
        parts = v
    else:
        parts = re.split(r"[\n,;]+", v or "")
    out, seen = [], set()
    for p in parts:
        p = clean_text(p, item_len)
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out[:max_items]


def parse_event(form) -> dict:
    g = (lambda k: form.get(k, "") if hasattr(form, "get") else "")
    errors = {}
    name = clean_text(g("event_name"), 120)
    college = clean_text(g("college"), 120)
    city = clean_text(g("city"), 80)
    event_type = clean_text(g("event_type"), 60) or "technology"
    country = clean_text(g("country"), 60) or "India"
    description = clean_text(g("description"), 600)
    benefits = clean_text(g("benefits"), 400)
    if not name:
        errors["event_name"] = "required"
    if not college:
        errors["college"] = "required"
    if not city:
        errors["city"] = "required"
    try:
        d = date.fromisoformat(clean_text(g("event_date"), 10))
    except ValueError:
        d = None
        errors["event_date"] = "use YYYY-MM-DD"
    end_raw = clean_text(g("event_end_date"), 10)
    end = None
    if end_raw:
        try:
            end = date.fromisoformat(end_raw)
            if d and end < d:
                errors["event_end_date"] = "must be on or after the event date"
        except ValueError:
            errors["event_end_date"] = "use YYYY-MM-DD, or leave empty for a one-day event"
    try:
        att = int(clean_text(g("expected_attendance"), 7))
        if not 10 <= att <= 100000:
            raise ValueError
    except ValueError:
        att = None
        errors["expected_attendance"] = "enter a number between 10 and 100000"
    audience = split_list(g("audience"))
    reqs = split_list(g("sponsorship_requirements"))
    if not audience:
        errors["audience"] = "add at least one audience"
    if not reqs:
        errors["sponsorship_requirements"] = "add at least one sponsorship type"
    if errors:
        raise ValidationError(errors)
    return {
        "name": name, "college": college, "city": city, "country": country, "event_date": d.isoformat(),
        "event_end_date": end.isoformat() if end and end != d else None,
        "expected_attendance": att, "event_type": event_type, "description": description, "benefits": benefits,
        "audience": audience, "requirements": reqs,
        "categories": split_list(g("categories")), "keywords": split_list(g("keywords")),
    }
