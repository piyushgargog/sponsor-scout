"""ListSearchProvider: serves a hand-curated CSV of company websites instead of calling a paid search API."""
import csv

from .base import SearchError, SearchProvider, SearchResult


class ListSearchProvider(SearchProvider):
    """Returns every row of the list for any query. A fixed list has no ranking, so `limit` is ignored;
    discovery dedupes by domain and the LLM classifier still filters for relevance. Columns: name,url,note."""
    name = "list"

    def __init__(self, path: str):
        if not path:
            raise SearchError("SEARCH_PROVIDER=list requires SEARCH_LIST_FILE (a CSV with columns name,url,note)")
        try:
            with open(path, encoding="utf-8", newline="") as f:
                rows = [r for r in csv.DictReader(f) if (r.get("url") or "").strip()]
        except OSError as e:
            raise SearchError(f"cannot read SEARCH_LIST_FILE '{path}': {e}")
        if not rows:
            raise SearchError(f"SEARCH_LIST_FILE '{path}' has no rows with a url")
        self.results = [SearchResult((r.get("name") or "").strip(), r["url"].strip(), (r.get("note") or "").strip()) for r in rows]

    def search(self, query, *, limit=10, country=None):
        return list(self.results)
