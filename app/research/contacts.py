"""Contact discovery from emails that literally appear on fetched pages. Never guesses addresses."""
import re
from urllib.parse import unquote, urlparse

from ..normalize import is_valid_email, normalize_contact_name, normalize_domain, normalize_email

EXCLUDE_LOCAL = re.compile(r"^(no-?reply|donotreply|privacy|legal|abuse|security|postmaster|webmaster|dpo|gdpr|compliance|billing|support|careers?|jobs|hr|recruit\w*|press|noc|unsubscribe|help)$", re.I)
# Inboxes that never handle sponsorship, also when combined with other words (billing-support@, report-fraud@, cloudsupport@).
EXCLUDE_TOKEN = re.compile(r"^(no-?reply|donotreply|privacy|legal|abuse|fraud|security|postmaster|webmaster|gdpr|compliance|billing|\w*support|"
                           r"careers?|jobs|recruit\w*|unsubscribe|bounces?|mailer-daemon|invoices?|refunds?)$", re.I)
# Link/button text that the page puts next to an address; it is not a person's name.
UI_WORDS = {"contact", "contacts", "us", "email", "e-mail", "mail", "learn", "more", "report", "abuse", "talk", "to", "enterprise", "reach",
            "at", "press", "inquiries", "enquiries", "here", "click", "write", "get", "in", "touch", "partnerships", "partner", "support",
            "sales", "hello", "help", "info", "team", "send", "message", "our", "the", "for", "and", "or", "media", "careers", "join",
            "sponsorship", "sponsorships", "marketing", "community", "events", "event", "business", "development", "partnership",
            "general", "enquiry", "inquiry", "office", "admin", "m"}
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


def _brand(domain: str | None) -> str:
    return (normalize_domain(domain) or "").split(".")[0]


def contact_is_usable(email: str, company_domain: str) -> bool:
    """A sponsorship contact must be a mailbox of this company: its domain carries the company's brand (so sample
    addresses like hello@company.com or bot@example.com and other companies' addresses are dropped), and it is not an
    abuse/billing/support-type inbox."""
    if not email or "@" not in email:
        return False
    local, domain = email.lower().rsplit("@", 1)
    if EXCLUDE_LOCAL.match(local) or any(EXCLUDE_TOKEN.match(t) for t in re.split(r"[-._]", local) if t):
        return False
    return bool(_brand(company_domain)) and _brand(domain) == _brand(company_domain)


def _person_name(text: str | None) -> str | None:
    name = normalize_contact_name(text)
    words = name.split()
    if not 1 <= len(words) <= 4 or any(w.lower() in UI_WORDS or any(ch.isdigit() for ch in w) for w in words):
        return None
    return name


def discover_contacts(pages, company_domain: str, people: list[dict]) -> list[dict]:
    """Returns contacts sorted best-first: {name, role, department, email, source_url, confidence, priority}."""
    found = {}
    for pg in pages:
        path = urlparse(pg.url).path.lower()
        on_relevant = any(k in path for k in RELEVANT_PAGE)
        for em in pg.emails:
            raw = unquote(em["email"]).strip().strip(".,;:")
            ne = normalize_email(raw)
            if not ne or not is_valid_email(raw.lower()) or not contact_is_usable(ne, company_domain):
                continue
            local = ne.split("@")[0]
            same = normalize_domain(ne.split("@")[1]) == normalize_domain(company_domain)
            # person lookup: same-page people near the email, or the mailto anchor text
            name, role = _person_name(em["anchor"]), None
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
            if not same:  # same brand on another TLD (company.dev vs company.com)
                conf = min(conf * 0.8, 0.7)
            cand = {"name": name, "role": role, "department": dept, "email": ne, "source_url": pg.url,
                    "confidence": round(min(conf, 0.95), 2), "priority": prio}
            prev = found.get(ne)
            if not prev or cand["confidence"] > prev["confidence"]:
                found[ne] = cand
    return sorted(found.values(), key=lambda c: (c["priority"], -c["confidence"], c["email"]))
