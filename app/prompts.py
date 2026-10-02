"""Prompt builders + JSON schemas. Prompts embed all context so any real LLM provider can use them."""
import json

SYSTEM_RULES = (
    "You are a careful research and outreach assistant for a college event sponsorship team. "
    "Never invent facts, names, numbers, URLs or email addresses. Use only information given in the prompt. "
    "If something is not in the provided material, say 'unknown' or omit it."
)

DISCOVERY_QUERIES_SCHEMA = {"type": "object", "required": ["queries"], "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
CLASSIFY_SCHEMA = {"type": "object", "required": ["companies"], "properties": {"companies": {"type": "array", "items": {
    "type": "object", "required": ["domain", "is_company", "relevance"],
    "properties": {"domain": {"type": "string"}, "is_company": {"type": "boolean"}, "company_name": {"type": "string"},
                   "industry": {"type": "string"}, "description": {"type": "string"}, "reason_relevant": {"type": "string"},
                   "relevance": {"type": "number"}}}}}}
CLAIM = {"type": "object", "required": ["text", "quote", "source_url"],
         "properties": {"text": {"type": "string"}, "quote": {"type": "string"}, "source_url": {"type": "string"}}}
SYNTHESIS_SCHEMA = {"type": "object", "required": ["summary"], "properties": {
    "summary": {"type": "string"}, "industry": {"type": "string"},
    "products": {"type": "array", "items": CLAIM}, "launches": {"type": "array", "items": CLAIM}}}
EMAIL_SCHEMA = {"type": "object", "required": ["subject", "body", "personalization", "cta", "suggested_ask"], "properties": {
    "subject": {"type": "string"}, "body": {"type": "string"}, "cta": {"type": "string"}, "suggested_ask": {"type": "string"},
    "personalization": {"type": "array", "items": {"type": "object", "required": ["sentence", "fact_ids"], "properties": {
        "sentence": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}}}}}}


def event_summary(ev: dict) -> dict:
    return {k: ev[k] for k in ("name", "college", "city", "country", "event_date", "expected_attendance", "event_type",
                               "audience", "requirements", "categories", "keywords") if k in ev}


def discovery_queries_prompt(ev: dict, n: int = 10) -> str:
    return (f"Write {n} diverse web-search queries to find companies that could sponsor this college event "
            "(cash, credits, swag, speakers, workshops). Cover different company categories (AI, developer tools, cloud, "
            "SaaS, security, edtech, databases, hosting, fintech for students, hardware, developer-hiring startups) and "
            "include queries for companies with a history of sponsoring student hackathons or university events. "
            "Do NOT name specific companies.\n\nEVENT:\n" + json.dumps(event_summary(ev), ensure_ascii=False))


def classify_prompt(ev: dict, candidates: list[dict]) -> str:
    cands = [{"domain": c["domain"], "title": c["title"], "snippets": c["snippets"]} for c in candidates]
    return ("For each search-result domain decide if it is a real company (not a news site, directory, blog or social "
            "profile) that could plausibly sponsor the event. Use ONLY the title/snippets. relevance is 0..1. "
            "reason_relevant must cite what the snippets actually say.\n\nEVENT:\n" +
            json.dumps(event_summary(ev), ensure_ascii=False) + "\n\nCANDIDATES:\n" + json.dumps(cands, ensure_ascii=False))


def synthesis_prompt(company: dict, pages: list[dict]) -> str:
    blocks = "\n\n".join(f"[SOURCE {p['url']}]\n{p['text'][:3500]}" for p in pages)
    return ("Summarize this company from the fetched pages only. For every product and recent launch you list, "
            "provide `quote`: an EXACT, verbatim sentence copied from the page, and `source_url`: the page it came from. "
            "If a fact is not on the pages, omit it. summary is 1-2 neutral sentences.\n\nCOMPANY: " + company["name"] +
            "\n\nPAGES:\n" + blocks)


def email_prompt(ev: dict, company: dict, contact: dict, facts: list[dict], sender: dict, ask_options: list[str]) -> str:
    fact_lines = "\n".join(f"{f['id']}: {f['text']}  (source: {f['source_url']})" for f in facts)
    return (
        "Write a short, specific cold email (100-180 words, excluding greeting/sign-off) asking a company to sponsor a college event.\n"
        "RULES:\n"
        "- Every statement about the company MUST come from the VERIFIED FACTS below; cite them via fact ids.\n"
        "- Do not invent numbers, products, names, partnerships or URLs. Do not use 'Dear Sir/Madam', 'hope this finds you well', hype, urgency or exclamation marks.\n"
        "- Greeting: use the contact's first name only if a name is given, otherwise 'Hi <Company> team,'.\n"
        "- No links. No signature (added later). Plain, human, direct tone. One clear CTA and one concrete sponsorship ask chosen from the ask options.\n"
        "- `personalization` lists each sentence in the body that states something specific about the company, with the fact ids that support it and a one-line `why`.\n\n"
        f"EVENT: {json.dumps(event_summary(ev), ensure_ascii=False)}\n"
        f"ORGANISER BENEFITS TO OFFER: {ev.get('benefits') or 'logo placement and access to attendees'}\n"
        f"COMPANY: {company['name']} ({company.get('industry') or 'unknown'})\n"
        f"CONTACT: {json.dumps({'name': contact.get('name'), 'role': contact.get('role')}, ensure_ascii=False)}\n"
        f"ASK OPTIONS: {json.dumps(ask_options)}\n"
        f"SENDER: {sender.get('name')} ({sender.get('role')})\n\nVERIFIED FACTS:\n{fact_lines}\n")
