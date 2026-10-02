"""SQLite data layer (WAL, thread-local connections) + relational schema.

Duplicate protection lives in the schema:
  * companies: UNIQUE(domain), UNIQUE(normalized_name)
  * contacts:  UNIQUE(company_id, normalized_email)
  * leads:     UNIQUE(event_id, company_id)
  * email_sends: partial UNIQUE indexes -> one live send per mailbox, one per (campaign, company)
"""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, college TEXT NOT NULL, city TEXT NOT NULL, country TEXT NOT NULL DEFAULT 'India',
  event_date TEXT NOT NULL, event_end_date TEXT, expected_attendance INTEGER NOT NULL, event_type TEXT NOT NULL,
  description TEXT, benefits TEXT,
  audience_json TEXT NOT NULL, requirements_json TEXT NOT NULL,
  categories_json TEXT NOT NULL DEFAULT '[]', keywords_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, normalized_name TEXT NOT NULL, domain TEXT NOT NULL, website TEXT NOT NULL,
  industry TEXT, description TEXT, reason_relevant TEXT, source_urls_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(domain), UNIQUE(normalized_name)
);
CREATE TABLE IF NOT EXISTS research_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  url TEXT NOT NULL, title TEXT, kind TEXT, fetched_at TEXT NOT NULL, content_hash TEXT, excerpt TEXT,
  UNIQUE(company_id, url)
);
CREATE TABLE IF NOT EXISTS company_research(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  research_version TEXT NOT NULL, status TEXT NOT NULL,
  profile_json TEXT, facts_json TEXT, stats_json TEXT, error TEXT, researched_at TEXT NOT NULL,
  UNIQUE(company_id, research_version)
);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT, role TEXT, department TEXT, email TEXT NOT NULL, normalized_email TEXT NOT NULL,
  source_url TEXT NOT NULL, confidence REAL NOT NULL, priority INTEGER NOT NULL DEFAULT 9,
  created_at TEXT NOT NULL,
  UNIQUE(company_id, normalized_email)
);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
  name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE', daily_limit INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  fit_score INTEGER, score_json TEXT,
  research_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
  contact_status TEXT NOT NULL DEFAULT 'unknown',
  email_status TEXT NOT NULL DEFAULT 'NONE',
  primary_contact_id INTEGER REFERENCES contacts(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(event_id, company_id)
);
CREATE TABLE IF NOT EXISTS email_drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id),
  subject TEXT NOT NULL, body TEXT NOT NULL,
  personalization_json TEXT, evidence_json TEXT, cta TEXT, suggested_ask TEXT, quality_json TEXT,
  status TEXT NOT NULL, model TEXT, version INTEGER NOT NULL DEFAULT 1,
  approved_by TEXT, approved_at TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_drafts_lead ON email_drafts(lead_id);
CREATE TABLE IF NOT EXISTS email_sends(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id INTEGER NOT NULL REFERENCES email_drafts(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  contact_id INTEGER REFERENCES contacts(id),
  to_email TEXT NOT NULL, normalized_email TEXT NOT NULL,
  status TEXT NOT NULL, gmail_message_id TEXT, thread_id TEXT, sender_account TEXT, error TEXT,
  created_at TEXT NOT NULL, sent_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_send_email_live ON email_sends(normalized_email)
  WHERE status IN ('SENDING','SENT','REPLIED','BOUNCED');
CREATE UNIQUE INDEX IF NOT EXISTS uq_send_campaign_company_live ON email_sends(campaign_id, company_id)
  WHERE status IN ('SENDING','SENT','REPLIED','BOUNCED');
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED',
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
  error TEXT, result_json TEXT, event_id INTEGER, label TEXT, dedupe_key TEXT,
  run_after TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, run_after);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT, entity_id INTEGER, details_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_logs(entity, entity_id);
CREATE TABLE IF NOT EXISTS oauth_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, email TEXT NOT NULL,
  refresh_token_enc TEXT, access_token_enc TEXT, expires_at TEXT, scope TEXT,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  UNIQUE(provider, email)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self.init()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            self._local.depth = 0
        return c

    def init(self):
        c = self._conn()
        c.executescript(SCHEMA)
        # idempotent column additions for databases created before the column existed
        if "event_end_date" not in {r[1] for r in c.execute("PRAGMA table_info(events)")}:
            c.execute("ALTER TABLE events ADD COLUMN event_end_date TEXT")

    def run(self, sql: str, params=()) -> int:
        cur = self._conn().execute(sql, params)
        return cur.lastrowid

    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def row(self, sql: str, params=()):
        r = self._conn().execute(sql, params).fetchone()
        return dict(r) if r else None

    def scalar(self, sql: str, params=()):
        r = self._conn().execute(sql, params).fetchone()
        return r[0] if r else None

    @contextmanager
    def tx(self):
        c = self._conn()
        if self._local.depth == 0:
            c.execute("BEGIN IMMEDIATE")
        self._local.depth += 1
        try:
            yield c
        except BaseException:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("ROLLBACK")
            raise
        else:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("COMMIT")

    def close(self):
        c = getattr(self._local, "conn", None)
        if c:
            c.close()
            self._local.conn = None
