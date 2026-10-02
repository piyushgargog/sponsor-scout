"""Deterministic offline provider used for demo mode and tests. It reads the structured `context`
passed with each task and builds outputs ONLY from that context (no invented facts)."""
import re

from .base import LLMProvider

DEV_VOCAB = {"developer", "developers", "api", "apis", "sdk", "cloud", "ai", "ml", "machine", "learning", "database",
             "databases", "hosting", "open-source", "opensource", "security", "cybersecurity", "devops", "serverless",
             "startup", "startups", "student", "students", "hackathon", "hackathons", "campus", "university", "edtech",
             "platform", "code", "coding", "engineering", "hardware", "iot", "fintech", "saas", "infrastructure"}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z\-]+", (text or "").lower()))


def _join_and(items):
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


class MockProvider(LLMProvider):
    name = "mock"
    model = "mock-deterministic"

    def generate(self, prompt, *, system=None, temperature=0.4, max_tokens=None) -> str:
        return "[mock] " + prompt[:200]

    def generate_json(self, prompt, *, system=None, schema=None, task=None, context=None, temperature=0.2):
        fn = getattr(self, f"_task_{task}", None)
        if not fn:
            return {}
        return fn(context or {})

    # ---- tasks -----------------------------------------------------------
    def _task_discovery_queries(self, ctx):
        ev = ctx["event"]
        cats = ev.get("categories") or ["developer tools", "cloud platform", "AI API", "cybersecurity", "edtech",
                                       "database", "hosting", "fintech students", "hardware developer boards", "startup hiring developers"]
        place = ev.get("country") or "India"
        qs = [f"{c} company sponsor student hackathon {place}" for c in cats[:10]]
        qs.append(f"companies that sponsor university tech events {place}")
        for kw in (ev.get("keywords") or [])[:3]:
            qs.append(f"{kw} sponsor college fest")
        return {"queries": qs}

    def _task_candidate_classify(self, ctx):
        out = []
        for c in ctx["candidates"]:
            blob = " ".join([c.get("title", "")] + c.get("snippets", []) + [str(c.get("extra", {}).get("industry", ""))])
            hits = sorted(_tokens(blob) & DEV_VOCAB)
            rel = 0.1 if not hits else min(0.95, 0.45 + 0.1 * len(hits))
            name = re.split(r"\s[\|\-–—:]\s", c.get("title", "") or c["domain"])[0].strip() or c["domain"]
            industry = c.get("extra", {}).get("industry") or "unknown"
            desc = (c.get("snippets") or [""])[0]
            reason = (f"Public description mentions {', '.join(hits[:4])}, which overlaps with the event audience."
                      if hits else "No overlap found with the event audience in the search snippets.")
            out.append({"domain": c["domain"], "is_company": True, "company_name": name, "industry": industry,
                        "description": desc, "reason_relevant": reason, "relevance": rel})
        return {"companies": out}

    def _task_research_synthesis(self, ctx):
        pages = ctx.get("pages", [])
        home = pages[0] if pages else {"text": "", "url": ""}
        sents = re.split(r"(?<=[.!?])\s+", home["text"])
        summary = " ".join(s.strip() for s in sents[:2]) if home["text"] else "unknown"
        products, launches = [], []
        prod_rx = re.compile(r"\b(offers?|provides?|platform|api|tool|database|cloud|builds?|gives|helps|boards?|accounts?|courses?)\b", re.I)
        launch_rx = re.compile(r"\b(launch(ed|es)|announc(ed|es)|introduc(ed|es)|released?)\b", re.I)
        for p in pages:
            for s in re.split(r"(?<=[.!?])\s+", p["text"]):
                s = s.strip()
                if not (25 <= len(s) <= 300):
                    continue
                if launch_rx.search(s) and len(launches) < 3:
                    launches.append({"text": s, "quote": s, "source_url": p["url"]})
                elif prod_rx.search(s) and len(products) < 4 and p["url"] == home["url"]:
                    products.append({"text": s, "quote": s, "source_url": p["url"]})
        return {"summary": summary, "industry": ctx.get("industry") or "unknown", "products": products, "launches": launches}

    def _task_email(self, ctx):
        ev, co, ct, facts = ctx["event"], ctx["company"], ctx["contact"], ctx["facts"]
        sender = ctx.get("sender", {})
        if not facts:
            return {"subject": "", "body": "", "personalization": [], "cta": "", "suggested_ask": ""}
        top = facts[0]
        second = facts[1] if len(facts) > 1 else None
        name = ct.get("name")
        greet = f"Hi {name.split()[0]}," if name else f"Hi {co['name']} team,"
        aud = _join_and(ev["audience"][:3])
        ask = ctx.get("suggested_ask") or (ev["requirements"][0] if ev["requirements"] else "a sponsorship")
        s1 = f'I came across this on your site: "{top["text"]}"'
        pers = [{"sentence": s1, "fact_ids": [top["id"]], "why": "Direct statement from the company's own page that shows relevant student/community activity."}]
        parts = [greet, "", s1, "",
                 f"I'm on the organizing team for {ev['name']}, a {ev['event_type']} event at {ev['college']} in {ev['city']} "
                 f"on {ev['date_human']}, expecting about {ev['expected_attendance']} attendees, mostly {aud}."]
        if second:
            s2 = f'We also noticed this: "{second["text"]}" That is why we think there is a natural overlap with the people who will be in the room.'
            pers.append({"sentence": s2, "fact_ids": [second["id"]], "why": "Second verified signal supporting the audience overlap."})
            parts += ["", s2]
        else:
            parts += ["", "We think there is a natural overlap between your work and the people who will be in the room."]
        parts += ["", f"We would like to explore {ask} from {co['name']}. In return we can offer {ev['benefits']}.",
                  "", "Would you be open to a short conversation in the next couple of weeks? If this is not relevant or you are not the right person, a one-line reply is enough and I will not follow up."]
        cta = "Would you be open to a short conversation in the next couple of weeks?"
        return {"subject": f"Partnership opportunity: {ev['name']} x {co['name']}", "body": "\n".join(parts),
                "personalization": pers, "cta": cta, "suggested_ask": ask}
