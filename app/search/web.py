"""WebSearchProvider: thin adapters over commercial search APIs (no scraping of search engines)."""
import requests

from ..logging_setup import log_event, redact
from .base import SearchError, SearchProvider, SearchResult

COUNTRY_CODES = {"india": "IN", "united states": "US", "usa": "US", "united kingdom": "GB", "uk": "GB", "canada": "CA",
                 "australia": "AU", "germany": "DE", "singapore": "SG"}


class WebSearchProvider(SearchProvider):
    name = "web"

    def __init__(self, backend: str, *, brave_key: str = "", google_key: str = "", google_cx: str = "", session=None):
        self.backend = backend
        self.brave_key, self.google_key, self.google_cx = brave_key, google_key, google_cx
        self.http = session or requests.Session()
        if backend == "brave" and not brave_key:
            raise SearchError("SEARCH_PROVIDER=brave requires BRAVE_API_KEY")
        if backend == "google_cse" and not (google_key and google_cx):
            raise SearchError("SEARCH_PROVIDER=google_cse requires GOOGLE_CSE_KEY and GOOGLE_CSE_CX")
        if backend not in ("brave", "google_cse"):
            raise SearchError(f"unknown search backend '{backend}'")

    def search(self, query, *, limit=10, country=None):
        cc = COUNTRY_CODES.get((country or "").lower())
        try:
            if self.backend == "brave":
                r = self.http.get("https://api.search.brave.com/res/v1/web/search", timeout=20,
                                  headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
                                  params={"q": query, "count": min(limit, 20), **({"country": cc} if cc else {})})
                if r.status_code != 200:
                    raise SearchError(f"Brave search HTTP {r.status_code}: {redact(r.text[:150])}")
                items = (r.json().get("web") or {}).get("results", [])
                res = [SearchResult(i.get("title", ""), i.get("url", ""), i.get("description", "")) for i in items]
            else:
                r = self.http.get("https://www.googleapis.com/customsearch/v1", timeout=20,
                                  params={"key": self.google_key, "cx": self.google_cx, "q": query, "num": min(limit, 10)})
                if r.status_code != 200:
                    raise SearchError(f"Google CSE HTTP {r.status_code}: {redact(r.text[:150])}")
                res = [SearchResult(i.get("title", ""), i.get("link", ""), i.get("snippet", "")) for i in r.json().get("items", [])]
        except requests.RequestException as e:
            raise SearchError(f"search network error: {type(e).__name__}") from e
        log_event("search_request", backend=self.backend, results=len(res))
        return [x for x in res if x.url][:limit]
