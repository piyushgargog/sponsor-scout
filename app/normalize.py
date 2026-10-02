"""Normalization used for duplicate detection (company name, domain, email)."""
import re
import unicodedata
from urllib.parse import urlparse

LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation",
                  "gmbh", "co", "company", "plc", "llp", "pte", "sa", "ag", "bv", "oy"}
MULTI_TLDS = {"co.uk", "org.uk", "ac.uk", "co.in", "org.in", "ac.in", "net.in", "edu.in", "gov.in", "com.au",
              "co.nz", "co.jp", "com.br", "com.sg", "co.za"}
SHARED_HOSTS = {"github.io", "vercel.app", "netlify.app", "herokuapp.com", "pages.dev", "web.app",
                "firebaseapp.com", "notion.site", "substack.com", "wixsite.com", "carrd.co", "framer.app"}
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
PLACEHOLDER_TLDS = {"example", "invalid", "test", "localhost", "local"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "domain.com", "email.com", "company.com", "yourcompany.com",
                       "yourdomain.com", "mycompany.com", "acme.com", "acme.io", "acme.org", "sample.com", "website.com", "mysite.com"}


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = s.split()
    while len(toks) > 1 and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def normalize_domain(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    raw = url_or_host.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return None
    labels = host.split(".")
    last2 = ".".join(labels[-2:])
    if last2 in MULTI_TLDS or last2 in SHARED_HOSTS:
        keep = 3
    else:
        keep = 2
    return ".".join(labels[-keep:]) if len(labels) >= keep else host


def is_valid_email(email: str | None) -> bool:
    if not email or not isinstance(email, str):
        return False
    e = email.strip().lower()
    if len(e) > 254 or ".." in e or any(c in e for c in "\r\n\t ,;<>"):
        return False
    if not _EMAIL_RE.match(e):
        return False
    local, domain = e.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith("."):
        return False
    tld = domain.rsplit(".", 1)[1]
    return len(tld) >= 2 and tld.isalpha()


def is_placeholder_email(email: str) -> bool:
    """True for reserved/placeholder domains (example.*, *.test ...). Blocked when sending for real."""
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in PLACEHOLDER_DOMAINS or domain.rsplit(".", 1)[-1] in PLACEHOLDER_TLDS


def normalize_email(email: str | None) -> str | None:
    """Lower-case, strip +tags (and dots for gmail) so variants of one mailbox dedupe. None if invalid."""
    if not email:
        return None
    e = email.strip().lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?")[0]
    if not is_valid_email(e):
        return None
    local, domain = e.rsplit("@", 1)
    local = local.split("+")[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if local else None


def normalize_contact_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'\-]", "", name, flags=re.UNICODE)).strip().title()


def same_site(url_domain: str | None, company_domain: str | None) -> bool:
    return bool(url_domain and company_domain and normalize_domain(url_domain) == normalize_domain(company_domain))
