"""Dependency container: wires providers chosen by configuration."""
from dataclasses import dataclass

from .db import Database
from .jobs import JobQueue
from .llm import get_llm
from .mailer.accounts import AccountStore
from .mailer.gmail import GmailProvider
from .mailer.mock import MockMailer
from .pipeline import Pipeline
from .repo import Repo
from .research.fetcher import FixtureFetcher, HttpFetcher
from .search.mock import MockSearchProvider
from .search.web import WebSearchProvider
from .sending import Sender


@dataclass
class Services:
    settings: object
    db: Database
    repo: Repo
    queue: JobQueue
    llm: object
    search: object
    fetcher: object
    mailer: object
    accounts: AccountStore
    pipeline: Pipeline
    sender: Sender


def build_services(settings, *, llm=None, search=None, fetcher=None, mailer=None) -> Services:
    db = Database(settings.db_path)
    repo, queue = Repo(db), JobQueue(db)
    llm = llm or get_llm(settings)
    if search is None:
        if settings.search_provider == "mock":
            search, fetcher = MockSearchProvider(), fetcher or FixtureFetcher()
        else:
            search = WebSearchProvider(settings.search_provider, brave_key=settings.brave_api_key,
                                       google_key=settings.google_cse_key, google_cx=settings.google_cse_cx)
    fetcher = fetcher or HttpFetcher(settings.fetch_delay_seconds)
    accounts = AccountStore(db, settings.secret_key)
    if mailer is None:
        mailer = GmailProvider(settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri, accounts) \
            if settings.mail_provider == "gmail" else MockMailer()
    pipeline = Pipeline(repo, settings, llm, search, fetcher, queue)
    sender = Sender(repo, settings, mailer, accounts, queue, pipeline)
    queue.register("discover", lambda p, j: pipeline.discover(p["event_id"], p.get("actor", "system")))
    queue.register("research", lambda p, j: pipeline.research_lead(p["lead_id"], p.get("force", False)))
    queue.register("generate_email", lambda p, j: {"draft_id": pipeline.generate_email(p["lead_id"], p.get("actor", "system"))})
    queue.register("send_email", lambda p, j: sender.send_now(p["draft_id"], p.get("actor", "system")))
    return Services(settings, db, repo, queue, llm, search, fetcher, mailer, accounts, pipeline, sender)
