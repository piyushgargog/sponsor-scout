"""Contact discovery from emails that literally appear on fetched pages. Never guesses addresses."""
import re
from urllib.parse import urlparse

from ..normalize import is_valid_email, normalize_contact_name, normalize_domain, normalize_email

EXCLUDE_LOCAL = re.compile(r"^(no-?reply|donotreply|privacy|legal|abuse|security|postmaster|webmaster|dpo|gdpr|compliance|billing|support|careers?|jobs|hr|recruit\w*|press|noc|unsubscribe)$", re.I)
DEPTS = [  # (department, priority, local-part regex)  -- priority follows the product brief order
    ("sponsorship", 1, re.compile(r"sponsor", re.I)),
    ("partnerships", 2, re.compile(r"partner|alliance|bizdev|bd$|business", re.I)),
    ("community", 3, re.compile(r"communit", re.I)),
    ("devrel", 4, re.compile(r"devrel|developer|dev-rel|advocate|dx$", re.I)),
    ("marketing", 5, re.compile(r"market|brand|events?|growth|pr$|media", re.I)),
    ("campus", 6, re.compile(r"campus|universit|student|education|academic|college|ambassador|programs?", re.I)),
    ("founder", 7, re.compile(r"founder|ceo|cofounder|co-founder|owner", re.I)),
]
GENERIC = re.compile(r"^(info|hello|hi|contact|team|enquiries|inquiries|admin|office|mail|general)$", re.I)
RELEVANT_PAGE = ("sponsor", "partner", "contact", "communit", "campus", "student")
ROLE_DEPT = [("sponsor", "sponsorship"), ("partner", "partnerships"), ("community", "community"), ("developer relations", "devrel"),
             ("marketing", "marketing"), ("campus", "campus"), ("founder", "founder"), ("ceo", "founder")]


def _dept_for(local: str, role: str | None):
    for dept, prio, rx in DEPTS:
        if rx.search(local):
            return dept, prio
    if role:
        r = role.lower()
        for kw, dept in ROLE_DEPT:
            if kw in r:
                return dept, next(p for d, p, _ in DEPTS if d == dept)
    return ("general", 8) if GENERIC.match(local) else ("other", 9)


def discover_contacts(pages, company_domain: str, people: list[dict]) -> list[dict]:
    """Returns contacts sorted best-first: {name, role, department, email, source_url, confidence, priority}."""
    found = {}
    for pg in pages:
        path = urlparse(pg.url).path.lower()
        on_relevant = any(k in path for k in RELEVANT_PAGE)
        for em in pg.emails:
            raw = em["email"].strip().strip(".,;:")
            ne = normalize_email(raw)
            if not ne or not is_valid_email(raw.lower()):
                continue
            local = ne.split("@")[0]
            if EXCLUDE_LOCAL.match(local):
                continue
            same = normalize_domain(ne.split("@")[1]) == normalize_domain(company_domain)
            # person lookup: same-page people near the email, or the mailto anchor text
            name, role = (normalize_contact_name(em["anchor"]) or None), None
            near = [p for p in people if p["source_url"] == pg.url and abs(p["pos"] - em["pos"]) < 220]
            if near and not name:
                nearest = min(near, key=lambda p: abs(p["pos"] - em["pos"]))
                name, role = nearest["name"], nearest["role"]
            elif name:
                match = next((p for p in people if p["name"].lower() == name.lower()), None)
                role = match["role"] if match else None
            dept, prio = _dept_for(local, role)
            conf = 0.40 + (0.20 if dept not in ("general", "other") else 0) + (0.15 if on_relevant else 0) \
                + (0.10 if em["mailto"] else 0) + (0.10 if same else 0)
            if not same:
                conf = min(conf * 0.6, 0.5)
            cand = {"name": name, "role": role, "department": dept, "email": ne, "source_url": pg.url,
                    "confidence": round(min(conf, 0.95), 2), "priority": prio}
            prev = found.get(ne)
            if not prev or cand["confidence"] > prev["confidence"]:
                found[ne] = cand
    return sorted(found.values(), key=lambda c: (c["priority"], -c["confidence"], c["email"]))
