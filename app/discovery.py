"""Lead discovery: LLM-written search queries -> SearchProvider -> candidate filtering -> LLM classification."""
from collections import OrderedDict

from .llm.base import LLMError
from .logging_setup import log_event
from .normalize import normalize_domain
from .prompts import CLASSIFY_SCHEMA, DISCOVERY_QUERIES_SCHEMA, SYSTEM_RULES, classify_prompt, discovery_queries_prompt
from .search.base import SearchError

# Hosts that are never sponsor candidates (social networks, news, directories, hackathon platforms, colleges).
NON_COMPANY_HOSTS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "reddit.com", "quora.com", "medium.com",
    "wikipedia.org", "github.com", "gitlab.com", "crunchbase.com", "g2.com", "capterra.com", "glassdoor.com", "indeed.com", "naukri.com",
    "techcrunch.com", "forbes.com", "businessinsider.com", "yourstory.com", "inc42.com", "indiatimes.com", "hindustantimes.com",
    "devpost.com", "unstop.com", "eventbrite.com", "meetup.com", "luma.com", "lu.ma", "devfolio.co", "pinterest.com", "tiktok.com",
    "google.com", "bing.com", "duckduckgo.com", "substack.com", "notion.site", "stackoverflow.com", "producthunt.com",
}
NON_COMPANY_SUFFIXES = (".edu", ".gov", ".ac.in", ".edu.in", ".gov.in", ".ac.uk", ".mil", ".nic.in")


def is_candidate_host(domain: str | None) -> bool:
    if not domain or domain in NON_COMPANY_HOSTS:
        return False
    return not domain.endswith(NON_COMPANY_SUFFIXES)


def fallback_queries(event: dict) -> list[str]:
    place = event.get("country") or "India"
    cats = event.get("categories") or ["developer tools", "cloud platform", "AI API", "cybersecurity", "edtech", "database", "hosting"]
    qs = [f"{c} company sponsors student hackathons {place}" for c in cats[:8]]
    qs.append(f"companies sponsoring college tech fests {event.get('city', '')} {place}".strip())
    return qs


def discover_candidates(event: dict, llm, search, max_candidates: int = 40, batch: int = 8) -> dict:
    try:
        qs = llm.generate_json(discovery_queries_prompt(event), system=SYSTEM_RULES, schema=DISCOVERY_QUERIES_SCHEMA,
                               task="discovery_queries", context={"event": event}).get("queries", [])
    except LLMError as e:
        log_event("discovery_query_fallback", error=str(e))
        qs = []
    qs = list(OrderedDict.fromkeys(q.strip() for q in qs if isinstance(q, str) and q.strip()))[:12] or fallback_queries(event)

    groups, errors = OrderedDict(), 0
    for q in qs:
        try:
            results = search.search(q, limit=10, country=event.get("country"))
        except SearchError as e:
            errors += 1
            log_event("search_failure", query=q, error=str(e))
            continue
        for r in results:
            dom = normalize_domain(r.url)
            if not is_candidate_host(dom):
                continue
            g = groups.setdefault(dom, {"domain": dom, "title": r.title, "snippets": [], "urls": [], "extra": r.extra, "hits": 0, "origin": None})
            g["hits"] += 1
            if r.snippet and r.snippet not in g["snippets"]:
                g["snippets"].append(r.snippet)
            if r.url not in g["urls"]:
                g["urls"].append(r.url)
            if g["origin"] is None or len(r.url) < len(g["origin"]):
                g["origin"] = r.url
    if errors == len(qs) and not groups:
        raise SearchError("All search queries failed; check SEARCH_PROVIDER credentials/network.")
    cands = sorted(groups.values(), key=lambda g: -g["hits"])[:max_candidates]

    accepted, rejected = [], []
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        try:
            out = llm.generate_json(classify_prompt(event, chunk), system=SYSTEM_RULES, schema=CLASSIFY_SCHEMA,
                                    task="candidate_classify", context={"event": event, "candidates": chunk}).get("companies", [])
        except LLMError as e:
            log_event("discovery_classify_failure", error=str(e))
            continue
        by_dom = {c["domain"]: c for c in chunk}
        for item in out:
            c = by_dom.get(normalize_domain(item.get("domain")))
            if not c:
                continue  # model referenced a domain that was not in the search results: ignore
            if item.get("is_company") and float(item.get("relevance", 0)) >= 0.4 and item.get("company_name"):
                from urllib.parse import urlparse
                u = urlparse(c["origin"])
                accepted.append({"company_name": item["company_name"].strip(), "website": f"{u.scheme}://{u.netloc}/",
                                 "industry": item.get("industry") or "unknown", "description": item.get("description") or (c["snippets"] or [""])[0],
                                 "reason_relevant": item.get("reason_relevant") or "", "source_urls": c["urls"][:5],
                                 "relevance": float(item["relevance"])})
            else:
                rejected.append(c["domain"])
    return {"queries": qs, "candidates": len(cands), "accepted": accepted, "rejected": rejected}
