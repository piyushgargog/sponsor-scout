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


FIT_ANALYSIS_SCHEMA = {"type": "object", "required": ["summary", "priority"], "properties": {
    "summary": {"type": "string"}, "priority": {"type": "string"}, "recommended_angle": {"type": "string"},
    "strengths": {"type": "array", "items": {"type": "object", "required": ["text", "fact_ids"], "properties": {
        "text": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}}}},
    "concerns": {"type": "array", "items": {"type": "string"}}}}
EMAIL_REVIEW_SCHEMA = {"type": "object", "required": ["unsupported_claims"], "properties": {
    "unsupported_claims": {"type": "array", "items": {"type": "object", "required": ["sentence", "reason"], "properties": {
        "sentence": {"type": "string"}, "reason": {"type": "string"}}}},
    "weak_personalization": {"type": "boolean"}, "notes": {"type": "string"}}}


def event_summary(ev: dict) -> dict:
    return {k: ev[k] for k in ("name", "college", "city", "country", "event_date", "event_end_date", "date_human", "expected_attendance",
                               "event_type", "description", "audience", "requirements", "categories", "keywords") if ev.get(k)}


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


IN_KIND_RULE = ("- This is an IN-KIND (barter) request: ask for products, credits, licences, prizes, swag, tool access or expert time in "
                "exchange for visibility. Do NOT ask for money, funding or payment, and do not mention budgets.\n")


def email_prompt(ev: dict, company: dict, contact: dict, facts: list[dict], sender: dict, ask_options: list[str]) -> str:
    from .emailgen import is_in_kind
    fact_lines = "\n".join(f"{f['id']}: {f['text']}  (source: {f['source_url']})" for f in facts)
    return (
        "Write a short, specific cold email (100-180 words, excluding greeting/sign-off) asking a company to sponsor a college event.\n"
        "RULES:\n"
        "- Every statement about the company MUST come from the VERIFIED FACTS below; cite them via fact ids.\n"
        "- Do not invent numbers, products, names, partnerships or URLs. Do not use 'Dear Sir/Madam', 'hope this finds you well', hype, urgency or exclamation marks.\n"
        "- Greeting: use the contact's first name only if a name is given, otherwise 'Hi <Company> team,'.\n"
        + (IN_KIND_RULE if is_in_kind(ev) else "") +
        "- No links. No signature (added later). Plain, human, direct tone. One clear CTA and one concrete sponsorship ask chosen from the ask options.\n"
        "- `personalization` lists each sentence in the body that states something specific about the company, with the fact ids that support it and a one-line `why`.\n\n"
        f"EVENT: {json.dumps(event_summary(ev), ensure_ascii=False)}\n"
        f"ORGANISER BENEFITS TO OFFER: {ev.get('benefits') or 'logo placement and access to attendees'}\n"
        f"COMPANY: {company['name']} ({company.get('industry') or 'unknown'})\n"
        f"CONTACT: {json.dumps({'name': contact.get('name'), 'role': contact.get('role')}, ensure_ascii=False)}\n"
        f"ASK OPTIONS: {json.dumps(ask_options)}\n"
        f"SENDER: {sender.get('name')} ({sender.get('role')})\n\nVERIFIED FACTS:\n{fact_lines}\n")


def fit_analysis_prompt(ev: dict, company: dict, score: dict, facts: list[dict]) -> str:
    fact_lines = "\n".join(f"{f['id']}: {f['text']}  (source: {f['source_url']})" for f in facts)
    dims = {v["label"]: f"{v['score']}/{v['max']}" for v in score["breakdown"].values()}
    return (
        "You are advising a student organiser on whether and how to approach a company for sponsorship. "
        "A transparent rule-based score already exists; you do NOT change or re-score it. Write advisory commentary.\n"
        "RULES:\n"
        "- `strengths`: reasons this company fits the event. EACH must cite one or more fact ids from VERIFIED FACTS and may only "
        "restate what those facts say. A strength without a fact id will be discarded.\n"
        "- `concerns`: gaps or risks, including dimensions that scored 0 because information is unknown. Do not speculate about the company.\n"
        "- `priority`: exactly one of high, medium, low (how worthwhile it is to contact this lead first).\n"
        "- `recommended_angle`: one sentence on what to lead with, based only on VERIFIED FACTS. `summary`: at most 2 sentences.\n"
        "- Never invent facts, numbers, programs, people or URLs.\n\n"
        f"EVENT: {json.dumps(event_summary(ev), ensure_ascii=False)}\n"
        f"COMPANY: {company['name']} ({company.get('industry') or 'unknown'})\n"
        f"RULE-BASED SCORE: {score['score']}/100 {json.dumps(dims)}\n\nVERIFIED FACTS:\n{fact_lines or '(none)'}\n")


def email_review_prompt(ev: dict, body: str, facts: list[dict]) -> str:
    fact_lines = "\n".join(f"{f['id']}: {f['text']}" for f in facts)
    return (
        "Review this cold email as a strict fact-checker. List every sentence that states something about the COMPANY "
        "(its products, programs, history, locations, numbers or intentions) that is NOT supported by the VERIFIED FACTS. "
        "Claims about the event come from the EVENT brief and are allowed. Copy each flagged sentence EXACTLY from the email. "
        "Set weak_personalization=true if the email would read the same for any company. Do not rewrite the email.\n\n"
        f"EVENT: {json.dumps(event_summary(ev), ensure_ascii=False)}\n\nVERIFIED FACTS:\n{fact_lines}\n\nEMAIL:\n{body}\n")
