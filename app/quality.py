"""Pre-send quality gate. 'block' checks make a draft unsendable; 'warn' checks require explicit human acknowledgement."""
import re

from .emailgen import core_body
from .normalize import is_placeholder_email, is_valid_email

SPAM_PHRASES = ["act now", "limited time", "guaranteed", "100%", "risk-free", "once in a lifetime", "don't miss", "urgent", "exclusive offer",
                "click here", "free money", "winner", "congratulations", "no obligation", "amazing opportunity", "buy now"]
GENERIC_OPENERS = ["dear sir", "dear madam", "to whom it may concern", "hope this email finds you", "hope this finds you", "i hope you are doing well",
                   "we are organizing an amazing", "we are thrilled to announce", "we are excited to announce"]
URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def _digits(s):
    return re.sub(r"[^\d]", "", s)


def evaluate(draft: dict, event: dict, company: dict, contact: dict | None, facts: list[dict], fit_score: int | None,
             min_fit: int = 40, real_send: bool = False) -> dict:
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    body_full, subject = draft.get("body", ""), draft.get("subject", "")
    body = core_body(body_full)
    by_id = {f["id"]: f for f in facts}

    # --- recipient ---
    email = (contact or {}).get("email")
    if not email or not is_valid_email(email):
        add("recipient", "block", "No valid, publicly sourced contact email.")
    elif real_send and is_placeholder_email(email):
        add("recipient", "block", "Recipient uses a placeholder/reserved domain; it cannot be a real mailbox.")
    else:
        add("recipient", "pass", f"Sending to {email}.")
    if contact and contact.get("confidence") is not None and contact["confidence"] < 0.5:
        add("contact_confidence", "warn", f"Low contact confidence ({contact['confidence']:.2f}); verify the address.")

    # --- length ---
    words = len(body.split())
    if words < 40 or words > 300:
        add("length", "block", f"{words} words; far outside the 100-180 target.")
    elif not 100 <= words <= 180:
        add("length", "warn", f"{words} words; target is 100-180.")
    else:
        add("length", "pass", f"{words} words.")

    # --- personalization & evidence ---
    pers = draft.get("personalization") or []
    valid_pers = [p for p in pers if p.get("fact_ids") and all(i in by_id for i in p["fact_ids"])]
    if not valid_pers or draft.get("unknown_fact_ids"):
        if not valid_pers:
            add("evidence", "block", "No personalization claim is backed by a verified source fact.")
        else:
            add("evidence", "warn", f"Model cited unknown fact ids {draft['unknown_fact_ids']}; those claims were dropped.")
    else:
        add("evidence", "pass", f"{len(valid_pers)} personalized claim(s), each linked to a source URL.")
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    missing = [p["sentence"][:60] for p in valid_pers if norm(p["sentence"]) not in norm(body)]
    if missing:
        add("personalization_in_body", "warn", "A cited personalization sentence is no longer in the body (edited?): " + "; ".join(missing))

    # --- accuracy: numbers that are not in the event brief or verified facts ---
    allowed = " ".join([str(event.get("expected_attendance", "")), event.get("event_date", ""), event.get("name", ""),
                        event.get("date_human", "")] + [f["text"] for f in facts if f["id"] in {i for p in valid_pers for i in p["fact_ids"]}] + [event.get("benefits") or ""])
    allowed_d = _digits(allowed)
    unverified = []
    for tok in re.findall(r"\d[\d,\.]*", body):
        d = _digits(tok)
        if len(d) >= 2 and d not in allowed_d and not re.fullmatch(r"20\d\d", d):
            unverified.append(tok)
    if unverified:
        add("accuracy", "warn", f"Numbers not found in the event brief or cited facts: {', '.join(sorted(set(unverified))[:5])}.")
    else:
        add("accuracy", "pass", "No unverifiable numbers found.")
    first = (contact or {}).get("name")
    m = re.match(r"\s*(?:hi|hello|hey)\s+([^,\n]+),", body, re.I)
    if m:
        g = m.group(1).strip()
        ok = (first and g.lower() == first.split()[0].lower()) or g.lower().endswith("team") or g.lower() == (company["name"].lower())
        if not ok:
            add("greeting", "warn", f"Greeting names '{g}' but the verified contact is '{first or 'unknown'}'.")
    elif not re.match(r"\s*(hi|hello)\b", body, re.I):
        add("greeting", "warn", "Email does not open with a simple greeting.")

    # --- spam / tone ---
    low = (subject + " " + body).lower()
    hits = [p for p in SPAM_PHRASES if p in low]
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", body) if w not in {"B.TECH", "CTF", "API", "APIS"}]
    links = len(URL_RE.findall(body))
    spam = []
    if hits:
        spam.append("promotional phrases: " + ", ".join(hits))
    if body.count("!") > 1 or "!!" in body:
        spam.append("excess exclamation marks")
    if len(caps) >= 3:
        spam.append("shouting (ALL CAPS)")
    if links > 1:
        spam.append(f"{links} links")
    if len(subject) > 70 or (subject.isupper() and len(subject) > 8):
        spam.append("subject too long/shouty")
    opener = [g for g in GENERIC_OPENERS if g in low]
    if opener:
        spam.append("generic opener: " + opener[0])
    add("spam_risk", "warn" if spam else "pass", "; ".join(spam) if spam else "No spam-risk signals.")

    # --- relevance ---
    if fit_score is None:
        add("relevance", "warn", "Company has not been scored yet.")
    elif fit_score < min_fit:
        add("relevance", "warn", f"Fit score {fit_score} is below the {min_fit} threshold.")
    else:
        add("relevance", "pass", f"Fit score {fit_score}.")

    blocks = sum(c["status"] == "block" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    status = "blocked" if blocks else ("needs_review" if warns else "ok")
    return {"status": status, "score": max(0, 100 - 30 * blocks - 10 * warns), "checks": checks, "word_count": words}
