import re

from ..fixtures.mock_data import COMPANIES
from .base import SearchProvider, SearchResult

_STOP = {"company", "companies", "sponsor", "sponsors", "student", "students", "hackathon", "hackathons", "india", "college",
         "fest", "university", "tech", "events", "that", "the", "and", "for"}


class MockSearchProvider(SearchProvider):
    """Serves fictional demo companies (app/fixtures/mock_data.py) so the pipeline runs offline."""
    name = "mock"

    def search(self, query, *, limit=10, country=None):
        q = set(re.findall(r"[a-z][a-z\-]+", query.lower())) - _STOP
        out = []
        for c in COMPANIES:
            blob = set(re.findall(r"[a-z][a-z\-]+", " ".join(c["tags"] + [c["industry"]]).lower()))
            overlap = len(q & blob)
            generic = "sponsor" in query.lower() and "university" in query.lower()
            if overlap or generic:
                out.append((overlap, SearchResult(f"{c['name']} | {c['tagline']}", f"https://www.{c['domain']}/",
                                                  c["snippet"], {"industry": c["industry"]})))
        out.sort(key=lambda t: -t[0])
        return [r for _, r in out][:limit]
