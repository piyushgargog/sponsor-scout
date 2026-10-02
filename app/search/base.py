from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    extra: dict = field(default_factory=dict)  # provider-specific metadata (used by the mock only)


class SearchError(Exception):
    pass


class SearchProvider(ABC):
    name = "base"

    @abstractmethod
    def search(self, query: str, *, limit: int = 10, country: str | None = None) -> list[SearchResult]:
        ...
