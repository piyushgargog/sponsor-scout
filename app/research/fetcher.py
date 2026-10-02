"""Polite, SSRF-safe page fetching. Respects robots.txt, caps size/redirects, rate-limits per host."""
import hashlib
import ipaddress
import re
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from ..fixtures.mock_data import PAGES_BY_URL
from ..normalize import normalize_domain

UA = "SponsorOutreachResearchBot/0.1 (college event sponsorship research; respects robots.txt)"
MAX_BYTES = 1_500_000
_LOCAL_TAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}$")
_DOMAIN_HEAD = re.compile(r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,5}")
MAX_TEXT_CHARS = 400_000
MAX_AT_SIGNS = 300


def find_emails(text: str):
    """Linear-time email finder anchored on '@' (an unbounded regex backtracks quadratically on hostile pages).
    Yields (email, start_offset)."""
    n = 0
    i = text.find("@")
    while i != -1 and n < MAX_AT_SIGNS:
        n += 1
        left = _LOCAL_TAIL.search(text, max(0, i - 64), i)
        right = _DOMAIN_HEAD.match(text, i + 1, min(len(text), i + 256))
        if left and right:
            yield (left.group(0) + "@" + right.group(0)).rstrip("."), left.start()
        i = text.find("@", i + 1)
_BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "footer", "header", "nav"}
_SKIP = {"script", "style", "noscript", "svg", "template"}


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list = field(default_factory=list)      # (absolute_url, anchor_text)
    emails: list = field(default_factory=list)     # dicts: email, pos, mailto(bool), anchor
    fetched_at: str = ""

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


class _Parser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.title, self._in_title = base, "", False
        self.parts, self.links, self.mailtos = [], [], []
        self._skip, self._a = 0, None
        self._len = 0

    def _emit(self, s):
        self.parts.append(s)
        self._len += len(s)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self._emit("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._a = {"href": href, "text": [], "pos": self._len}

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._emit("\n")
        if tag == "a" and self._a:
            a, self._a = self._a, None
            txt = " ".join("".join(a["text"]).split())
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append({"email": href[7:].split("?")[0], "anchor": txt, "pos": a["pos"]})
            elif href and not href.lower().startswith(("javascript:", "#", "tel:")):
                self.links.append((urljoin(self.base, href), txt))

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self._emit(data)
        if self._a is not None:
            self._a["text"].append(data)


def parse_html(url: str, html: str) -> Page:
    p = _Parser(url)
    p.feed(html)
    raw = "".join(p.parts)
    text = "\n".join(" ".join(line.split()) for line in raw.split("\n") if line.strip())[:MAX_TEXT_CHARS]
    emails, seen = [], set()
    for m in p.mailtos:
        e = m["email"].strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            anchor = m["anchor"] if m["anchor"] and "@" not in m["anchor"] else ""
            emails.append({"email": e, "pos": max(text.find(m["anchor"]), 0) if m["anchor"] else 0, "mailto": True, "anchor": anchor})
    for e, pos in find_emails(text):
        if e.lower() not in seen:
            seen.add(e.lower())
            emails.append({"email": e, "pos": pos, "mailto": False, "anchor": ""})
    return Page(url=url, title=" ".join(p.title.split())[:200], text=text, links=p.links, emails=emails,
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def is_public_host(host: str) -> bool:
    """SSRF guard: every resolved address must be globally routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return bool(infos)


class Fetcher:
    def fetch(self, url: str):
        raise NotImplementedError


class FixtureFetcher(Fetcher):
    """Serves the fictional demo sites. Used when SEARCH_PROVIDER=mock."""

    def fetch(self, url):
        u = url.split("#")[0]
        html = PAGES_BY_URL.get(u) or PAGES_BY_URL.get(u.rstrip("/") + "/" if u.count("/") == 2 else u)
        return parse_html(u, html) if html else None


class HttpFetcher(Fetcher):
    def __init__(self, delay: float = 1.0, session=None, timeout: int = 10):
        self.delay, self.timeout = delay, timeout
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = UA
        self._last = {}
        self._robots = {}
        self._lock = threading.Lock()

    def _wait(self, host):
        with self._lock:
            wait = self._last.get(host, 0) + self.delay - time.time()
            self._last[host] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url, redirects=3):
        for _ in range(redirects + 1):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname or not is_public_host(p.hostname):
                return None
            self._wait(p.hostname)
            try:
                r = self.http.get(url, timeout=self.timeout, allow_redirects=False, stream=True)
            except requests.RequestException:
                return None
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                r.close()
                continue
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "text/html").lower():
                r.close()
                return None
            data = r.raw.read(MAX_BYTES + 1, decode_content=True)
            r.close()
            return url, data[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
        return None

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                if p.hostname and is_public_host(p.hostname):
                    self._wait(p.hostname)
                    r = self.http.get(base + "/robots.txt", timeout=self.timeout, allow_redirects=False)
                    if r.status_code == 200:
                        rp.parse(r.text[:200_000].splitlines())
                    elif r.status_code in (401, 403):
                        rp.parse(["User-agent: *", "Disallow: /"])
                    else:
                        rp.parse([])
                else:
                    rp.parse(["User-agent: *", "Disallow: /"])
            except requests.RequestException:
                rp.parse([])
            self._robots[base] = rp
        return rp.can_fetch(UA, url)

    def fetch(self, url):
        if not self.allowed(url):
            return None
        got = self._get(url)
        return parse_html(got[0], got[1]) if got else None


PAGE_KEYWORDS = [("sponsor", 10), ("partner", 9), ("student", 8), ("campus", 8), ("universit", 8), ("education", 7),
                 ("communit", 8), ("developer", 7), ("devrel", 7), ("hackathon", 8), ("startup", 5), ("about", 6),
                 ("contact", 9), ("team", 5), ("career", 4), ("jobs", 4), ("press", 3), ("blog", 3), ("news", 3), ("events", 6), ("india", 6)]
FALLBACK_PATHS = ["/about", "/contact", "/partners", "/community", "/students", "/careers"]


def classify_page_kind(url: str) -> str:
    path = urlparse(url).path.lower()
    for k in ("sponsor", "partner", "contact", "communit", "student", "campus", "career", "about", "team", "blog", "press", "event", "hackathon", "developer"):
        if k in path:
            return k
    return "home" if path in ("", "/") else "other"


def crawl_company(fetcher: Fetcher, website: str, max_pages: int = 8) -> list[Page]:
    """Fetch the homepage, then the most relevant same-site pages (sponsorship, partners, community, ...)."""
    domain = normalize_domain(website)
    root = website if "://" in website else "https://" + website
    home = fetcher.fetch(root)
    pages = [home] if home else []
    seen = {root.rstrip("/")}
    cands = []
    if home:
        for href, txt in home.links:
            if normalize_domain(href) != domain:
                continue
            clean = href.split("#")[0].split("?")[0].rstrip("/")
            if clean in seen:
                continue
            blob = (urlparse(clean).path + " " + txt).lower()
            score = max([w for k, w in PAGE_KEYWORDS if k in blob] or [0])
            if score:
                cands.append((score, clean))
    if not cands:
        base = root.rstrip("/")
        cands = [(5, base + p) for p in FALLBACK_PATHS]
    for _, url in sorted(cands, key=lambda t: -t[0]):
        if len(pages) >= max_pages:
            break
        if url in seen:
            continue
        seen.add(url)
        pg = fetcher.fetch(url)
        if pg:
            pages.append(pg)
    return pages
