"""Transparent, rule-based sponsorship fit score (0-100). The LLM never touches the score or the evidence.

Student Audience Fit 0-20 | Technology Relevance 0-20 | Sponsorship History 0-20
India Presence 0-15 | Developer/Community Focus 0-15 | Event Scale Fit 0-10
Anything not found in verified public pages scores 0 and is reported as 'unknown'.
"""
import re

RUBRIC = [("student_audience_fit", "Student Audience Fit", 20), ("technology_relevance", "Technology Relevance", 20),
          ("sponsorship_history", "Sponsorship History", 20), ("india_presence", "India Presence", 15),
          ("developer_community_focus", "Developer/Community Focus", 15), ("event_scale_fit", "Event Scale Fit", 10)]
STOP = {"the", "and", "for", "with", "students", "student", "tech", "technology", "fest", "event", "events", "cash", "sponsorship",
        "swag", "speakers", "workshop", "partners", "credits", "b.tech", "btech", "of", "in", "a", "an", "to", "on", "our", "we"}
DEV_TERMS = {"developer", "developers", "api", "apis", "sdk", "cloud", "ai", "ml", "machine", "learning", "database", "hosting",
             "open-source", "serverless", "security", "devops", "infrastructure", "platform", "startup", "startups", "iot", "hardware", "saas",
             "edtech", "fintech", "payments", "engineers", "coding"}


def _tok(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z\-]+", (text or "").lower()))


def _facts(facts, *cats):
    return [f for f in facts if any(c in f["categories"] for c in cats)]


def _ev(f, claim=None):
    return {"claim": claim or f["text"], "source": f["source_url"], "fact_id": f["id"]}


def score_lead(event: dict, company: dict, research: dict | None) -> dict:
    research = research or {}
    facts = research.get("facts", [])
    profile = research.get("profile", {})
    home_url = profile.get("homepage_url") or company.get("website")
    b, evidence = {}, []

    def put(key, pts, reasons, ev=()):
        label, mx = next((l, m) for k, l, m in RUBRIC if k == key)
        b[key] = {"label": label, "score": max(0, min(mx, pts)), "max": mx, "reasons": reasons}
        evidence.extend(ev)

    # 1. Student audience fit
    stud = _facts(facts, "student_program", "ambassador", "university_partnership")
    pts, rs, ev = 0, [], []
    if stud:
        pts += 8 + 4 * min(len(stud) - 1, 2)
        rs.append(f"{len(stud)} public statement(s) about student/campus programs.")
        ev += [_ev(f) for f in stud[:3]]
    if _facts(facts, "ambassador", "credits"):
        pts += 4
        rs.append("Offers student credits or an ambassador program.")
    if not stud:
        rs.append("unknown: no public student/campus program found on fetched pages.")
    put("student_audience_fit", pts, rs, ev)

    # 2. Technology relevance (overlap between event brief and company's own words)
    ev_terms = (_tok(" ".join(event.get("audience", []) + event.get("requirements", []) + [event.get("event_type", "")] + event.get("categories", []) + event.get("keywords", []))) - STOP)
    co_text = " ".join([company.get("industry") or "", company.get("description") or "", profile.get("summary") or "",
                        " ".join(p["text"] for p in profile.get("products", []))])
    co_terms = _tok(co_text)
    overlap, dev = sorted(ev_terms & co_terms), sorted(DEV_TERMS & co_terms)
    pts = 5 * min(len(overlap), 2) + 2 * min(len(dev), 5)
    rs = ([f"Company description overlaps the event brief on: {', '.join(overlap[:5])}."] if overlap else []) + \
         ([f"Developer/tech vocabulary in company description: {', '.join(dev[:5])}."] if dev else [])
    ev = [{"claim": f"Company profile: {(profile.get('summary') or company.get('description') or '')[:180]}", "source": home_url, "fact_id": None}] if (overlap or dev) and home_url else []
    if not rs:
        rs = ["unknown: no technology overlap found."]
    put("technology_relevance", pts, rs, ev)

    # 3. Sponsorship history
    sp = _facts(facts, "sponsorship", "hackathon")
    sp = [f for f in sp if "sponsorship" in f["categories"]] or sp
    n = len(sp)
    pts = 0 if n == 0 else 10 if n == 1 else 16 if n == 2 else 20
    rs = [f"{n} public statement(s) about sponsoring or hosting events/hackathons."] if n else ["unknown: no sponsorship history found."]
    put("sponsorship_history", pts, rs, [_ev(f) for f in sp[:3]])

    # 4. India presence
    delhi, india = _facts(facts, "delhi_ncr"), _facts(facts, "india")
    if delhi:
        pts, rs, ev = 15, ["Public mention of a Delhi/NCR presence or activity."], [_ev(delhi[0])]
    elif india:
        pts, rs, ev = 10 if any(re.search(r"\b(office|offices|headquarter|team|distribut|partners)", f["text"], re.I) for f in india) else 6, \
            ["Public mention of India presence/activity (not Delhi/NCR specifically)."], [_ev(india[0])]
    else:
        pts, rs, ev = 0, ["unknown: no India presence found."], []
    put("india_presence", pts, rs, ev)

    # 5. Developer/community focus
    dc = _facts(facts, "developer_program", "hackathon")
    pts = 0 if not dc else 7 + 4 * min(len(dc) - 1, 2)
    rs = [f"{len(dc)} public statement(s) about developer/community programs."] if dc else ["unknown: no developer/community program found."]
    put("developer_community_focus", pts, rs, [_ev(f) for f in dc[:2]])

    # 6. Event scale fit (size of audience vs. the kind of events the company already supports)
    att = int(event.get("expected_attendance") or 0)
    small_events = [f for f in sp if re.search(r"\b(student|campus|college|universit|community|meetup|fest)", f["text"], re.I)]
    if small_events and att >= 100:
        pts, rs, ev = 10, [f"Company already supports student/community-scale events; {att} attendees is in range."], [_ev(small_events[0])]
    elif sp and att >= 100:
        pts, rs, ev = 6, ["Company sponsors events, but not clearly student/community-scale ones."], [_ev(sp[0])]
    elif att >= 300:
        pts, rs, ev = 3, ["No sponsorship history found; audience size is the only supporting signal."], []
    else:
        pts, rs, ev = 0, ["unknown: not enough information to judge scale fit."], []
    put("event_scale_fit", pts, rs, ev)

    total = sum(v["score"] for v in b.values())
    reasons = [r for v in b.values() if v["score"] > 0 for r in v["reasons"][:1]]
    seen, uniq = set(), []
    for e in evidence:
        k = (e["claim"], e["source"])
        if e["source"] and k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"score": total, "breakdown": b, "reasons": reasons or ["No verified evidence of fit was found."], "evidence": uniq}
