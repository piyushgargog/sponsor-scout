"""Deterministic, verifiable extraction. Every fact is a verbatim sentence from a fetched page + its URL,
so evidence can never be invented by an LLM."""
import re

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

EVENT_WORDS = r"(hackathon|conference|fest|festival|summit|meetup|event|campus|universit|college|student|ctf|capture-the-flag|bootcamp)"
RULES = [
    ("sponsorship", re.compile(r"\b(sponsor(?:ed|ing|s|ship)?|co-?host(?:ed|s)?)\b", re.I), re.compile(EVENT_WORDS, re.I)),
    ("hackathon", re.compile(r"\b(hackathons?|hack ?days?|codefest|datathon|capture-the-flag|ctf)\b", re.I), None),
    ("student_program", re.compile(r"\b(students?|campus|universit(?:y|ies)|colleges?)\b", re.I),
     re.compile(r"\b(program|programme|credits?|free|discount|ambassador|club|scholarship|internships?|accounts?|learning path|plans?)\b", re.I)),
    ("university_partnership", re.compile(r"\b(universit(?:y|ies)|colleges?|labs?|institutes?)\b", re.I),
     re.compile(r"\b(partner(?:s|ed|ship)?|collaborat\w+|curriculum|supply|supports?)\b", re.I)),
    ("credits", re.compile(r"\b(credits?|free tier|startup program|grants?)\b", re.I), None),
    ("ambassador", re.compile(r"\bambassadors?\b", re.I), None),
    ("developer_program", re.compile(r"\b(developer relations|developer community|community program|community forum|devrel|open-source|office hours|workshops?|meetups?|demo days?|advocates?)\b", re.I), None),
    ("india", re.compile(r"\b(india|delhi|ncr|gurgaon|gurugram|noida|bengaluru|bangalore|mumbai|hyderabad|pune|chennai)\b", re.I), None),
    ("hiring", re.compile(r"\b(hiring|open roles|open positions|internships?|interns)\b", re.I), None),
    ("launch", re.compile(r"\b(launched|announced|introduced|released|unveiled)\b", re.I), None),
]
DELHI_RE = re.compile(r"\b(delhi|ncr|gurgaon|gurugram|noida)\b", re.I)
PRIORITY = ["sponsorship", "hackathon", "student_program", "university_partnership", "credits", "ambassador",
            "developer_program", "india", "launch", "hiring"]
PER_CAT_CAP = 4
ROLE_RE = (r"(?:Head of [A-Z][A-Za-z &]+|Director of [A-Z][A-Za-z &]+|Developer Relations(?: [A-Z][A-Za-z]+)*|"
           r"Community (?:Manager|Lead)|Partnerships? (?:Manager|Lead|Head)|Marketing (?:Manager|Lead|Head)|Founder|Co-?founder|CEO|CTO)")
PERSON_RE = re.compile(rf"\b([A-Z][a-z]+(?: [A-Z][a-z]+){{1,2}}),\s+({ROLE_RE})")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text) if 25 <= len(s.strip()) <= 400]


def categorize(sentence: str) -> list[str]:
    cats = []
    for name, rx, need in RULES:
        if rx.search(sentence) and (need is None or need.search(sentence)):
            cats.append(name)
    if "india" in cats and DELHI_RE.search(sentence):
        cats.append("delhi_ncr")
    # A bare "sponsor" mention with no event context should not count; handled by `need` above.
    return cats


def extract_facts(pages, max_facts: int = 40) -> list[dict]:
    """Returns [{id, categories, text, source_url}] with verbatim sentences, ordered by usefulness."""
    out, seen, per = [], set(), {}
    for pg in pages:
        for s in split_sentences(pg.text):
            key = re.sub(r"\W+", " ", s.lower()).strip()
            if key in seen or "@" in s:
                continue
            cats = categorize(s)
            if not cats:
                continue
            main = next((c for c in PRIORITY if c in cats), cats[0])
            if per.get(main, 0) >= PER_CAT_CAP:
                continue
            seen.add(key)
            per[main] = per.get(main, 0) + 1
            out.append({"categories": cats, "text": s, "source_url": pg.url, "_rank": PRIORITY.index(main) if main in PRIORITY else 99})
    out.sort(key=lambda f: f["_rank"])
    out = out[:max_facts]
    for i, f in enumerate(out, 1):
        f["id"] = f"F{i}"
        f.pop("_rank", None)
    return out


def extract_people(pages) -> list[dict]:
    out, seen = [], set()
    for pg in pages:
        for m in PERSON_RE.finditer(pg.text):
            name, role = m.group(1), m.group(2).strip()
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append({"name": name, "role": role, "source_url": pg.url, "pos": m.start(), "quote": m.group(0)})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u2018\u2019]", "'", re.sub(r"[\u201c\u201d]", '"', s))).strip().lower()


def verify_quote(quote: str, source_url: str, pages) -> bool:
    """A claim is accepted only if its quote appears verbatim (whitespace/case-insensitive) on the cited page."""
    q = _norm(quote or "")
    if len(q) < 15:
        return False
    for pg in pages:
        if pg.url.rstrip("/") == (source_url or "").rstrip("/"):
            return q in _norm(pg.text)
    return False
