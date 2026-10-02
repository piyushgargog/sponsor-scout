"""Personalized email generation with evidence tracking and structural validation."""
import re
from datetime import date

from .llm.base import LLMError
from .prompts import EMAIL_SCHEMA, email_prompt, SYSTEM_RULES

SIG_MARKER = "\n\n-- \n"
DEFAULT_BENEFITS = "logo placement across the event, a demo or workshop slot, and direct access to attendees"
ASK_BY_CATEGORY = [("credits", ["API credits", "cloud credits", "credits", "licence", "license", "subscription"]),
                   ("hackathon", ["prizes", "workshop partners", "cash sponsorship", "mentorship"]),
                   ("sponsorship", ["cash sponsorship", "prizes", "workshop partners"]),
                   ("student_program", ["swag", "free tool access", "subscription", "speakers"]),
                   ("developer_program", ["free tool access", "speakers", "workshop partners", "mentorship"])]
CASH_TERMS = ("cash", "money", "monetary", "fund", "stipend", "fee")
FACT_ORDER = ["sponsorship", "hackathon", "student_program", "credits", "university_partnership", "ambassador", "developer_program", "india", "launch"]


class EmailGenError(Exception):
    pass


def human_date_range(start: str, end: str | None) -> str:
    """'30 October 2026', '30-31 October 2026' or '30 October - 2 November 2026'."""
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end) if end else None
    except ValueError:
        return human_date(start)
    if not b or b <= a:
        return human_date(start)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}-{b.day} {b.strftime('%B %Y')}"
    return f"{a.day} {a.strftime('%B')} - {human_date(end)}" if a.year == b.year else f"{human_date(start)} - {human_date(end)}"


def human_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%B %Y')}"
    except ValueError:
        return iso


def select_facts(facts: list[dict], limit: int = 8) -> list[dict]:
    def rank(f):
        return min((FACT_ORDER.index(c) for c in f["categories"] if c in FACT_ORDER), default=99)
    useful = [f for f in facts if rank(f) < 99]
    return sorted(useful, key=rank)[:limit]


def suggest_ask(event: dict, facts: list[dict]) -> str:
    reqs = event.get("requirements") or []
    low = {r.lower(): r for r in reqs}
    cats = {c for f in facts for c in f["categories"]}
    for cat, prefs in ASK_BY_CATEGORY:
        if cat in cats:
            for p in prefs:
                for k, orig in low.items():
                    if p.lower() in k:
                        return orig
    return reqs[0] if reqs else "sponsorship"


def is_in_kind(event: dict) -> bool:
    """Barter event: none of the requested sponsorship types is money, so emails must not ask for payment."""
    reqs = " ".join(event.get("requirements") or []).lower()
    return bool(reqs) and not any(t in reqs for t in CASH_TERMS)


def event_context(event: dict) -> dict:
    e = dict(event)
    e["date_human"] = human_date_range(event["event_date"], event.get("event_end_date"))
    e["benefits"] = (event.get("benefits") or "").strip().rstrip(".") or DEFAULT_BENEFITS
    return e


def signature(sender: dict, event: dict) -> str:
    lines = [sender.get("name") or "", f"{sender.get('role') or ''}, {sender.get('org') or ''}".strip(", "), f"{event['name']}, {event['college']}"]
    return SIG_MARKER + "\n".join(l for l in lines if l.strip())


def strip_signature(body: str) -> str:
    return re.split(r"\n\s*--\s*\n", body, maxsplit=1)[0].rstrip()


def core_body(body: str) -> str:
    return strip_signature(body)


def generate_draft(llm, event: dict, company: dict, contact: dict, facts: list[dict], sender: dict) -> dict:
    if not facts:
        raise EmailGenError("No verified facts about this company are available, so a personalized email cannot be written.")
    ev = event_context(event)
    chosen = select_facts(facts)
    if not chosen:
        raise EmailGenError("No relevant verified facts (sponsorship, student or developer programs) were found for this company.")
    ask = suggest_ask(ev, chosen)
    prompt = email_prompt(ev, company, contact, chosen, sender, ev.get("requirements") or [])
    ctx = {"event": ev, "company": company, "contact": contact, "facts": chosen, "sender": sender, "suggested_ask": ask}
    try:
        out = llm.generate_json(prompt, system=SYSTEM_RULES, schema=EMAIL_SCHEMA, task="email", context=ctx, temperature=0.5)
    except LLMError as e:
        raise EmailGenError(f"Email generation failed: {e}") from e
    return finalize_draft(out, chosen, ev, sender, ask)


def finalize_draft(out: dict, chosen: list[dict], ev: dict, sender: dict, ask: str) -> dict:
    by_id = {f["id"]: f for f in chosen}
    subject = " ".join(str(out.get("subject", "")).split())[:150]
    body = strip_signature(str(out.get("body", "")).replace("\r\n", "\n")).strip()
    if not subject or not body:
        raise EmailGenError("The model returned an empty subject or body.")
    pers, unknown_ids = [], []
    for item in out.get("personalization") or []:
        ids = [i for i in item.get("fact_ids", []) if i in by_id]
        unknown_ids += [i for i in item.get("fact_ids", []) if i not in by_id]
        if ids and item.get("sentence"):
            pers.append({"sentence": item["sentence"].strip(), "fact_ids": ids, "why": (item.get("why") or "").strip()})
    used = {i for p in pers for i in p["fact_ids"]}
    evidence = [{"fact_id": i, "claim": by_id[i]["text"], "source": by_id[i]["source_url"]} for i in sorted(used, key=lambda x: int(x[1:]))]
    return {"subject": subject, "body": body + signature(sender, ev), "personalization": pers, "evidence": evidence,
            "cta": (out.get("cta") or "").strip(), "suggested_ask": (out.get("suggested_ask") or ask).strip(),
            "unknown_fact_ids": unknown_ids}
