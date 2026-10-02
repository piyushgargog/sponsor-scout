"""Data access helpers shared by pipeline, sending and web layers."""
import sqlite3

from .db import Database
from .normalize import normalize_company_name, normalize_domain, normalize_email, is_valid_email
from .util import jdump, jload, now_iso


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ---- audit -------------------------------------------------------------
    def audit(self, actor, action, entity=None, entity_id=None, **details):
        self.db.run("INSERT INTO audit_logs(ts,actor,action,entity,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                    (now_iso(), actor, action, entity, entity_id, jdump(details)))

    def audit_for(self, entity, entity_id, limit=50):
        return self.db.rows("SELECT * FROM audit_logs WHERE entity=? AND entity_id=? ORDER BY id DESC LIMIT ?", (entity, entity_id, limit))

    # ---- events / campaigns -----------------------------------------------
    def create_event(self, d: dict, daily_limit: int, actor="system") -> int:
        with self.db.tx():
            eid = self.db.run(
                "INSERT INTO events(name,college,city,country,event_date,expected_attendance,event_type,description,benefits,"
                "audience_json,requirements_json,categories_json,keywords_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["name"], d["college"], d["city"], d.get("country", "India"), d["event_date"], d["expected_attendance"],
                 d["event_type"], d.get("description"), d.get("benefits"), jdump(d["audience"]), jdump(d["requirements"]),
                 jdump(d.get("categories", [])), jdump(d.get("keywords", [])), now_iso()))
            self.db.run("INSERT INTO campaigns(event_id,name,daily_limit,created_at) VALUES(?,?,?,?)",
                        (eid, f"{d['name']} outreach", daily_limit, now_iso()))
            self.audit(actor, "event_created", "event", eid, name=d["name"])
        return eid

    @staticmethod
    def _event(r):
        if not r:
            return None
        r["audience"] = jload(r.pop("audience_json"), [])
        r["requirements"] = jload(r.pop("requirements_json"), [])
        r["categories"] = jload(r.pop("categories_json"), [])
        r["keywords"] = jload(r.pop("keywords_json"), [])
        return r

    def get_event(self, eid):
        return self._event(self.db.row("SELECT * FROM events WHERE id=?", (eid,)))

    def list_events(self):
        return [self._event(r) for r in self.db.rows("SELECT * FROM events ORDER BY id DESC")]

    def campaign_for_event(self, eid):
        return self.db.row("SELECT * FROM campaigns WHERE event_id=?", (eid,))

    # ---- companies ---------------------------------------------------------
    def find_company(self, domain, name):
        r = self.db.row("SELECT * FROM companies WHERE domain=?", (domain,)) if domain else None
        return r or self.db.row("SELECT * FROM companies WHERE normalized_name=?", (normalize_company_name(name),))

    def upsert_company(self, name, website, industry=None, description=None, reason=None, source_urls=None):
        """Returns (company_id, created). Dedupes on normalized domain, then normalized name."""
        domain = normalize_domain(website)
        if not domain:
            raise ValueError("company website has no valid domain")
        nn = normalize_company_name(name)
        if not nn:
            raise ValueError("company name is empty")
        ex = self.find_company(domain, name)
        if ex:
            self.db.run("UPDATE companies SET industry=COALESCE(NULLIF(industry,'unknown'),?,industry), description=COALESCE(description,?),"
                        " reason_relevant=COALESCE(reason_relevant,?) WHERE id=?", (industry, description, reason, ex["id"]))
            return ex["id"], False
        try:
            cid = self.db.run(
                "INSERT INTO companies(name,normalized_name,domain,website,industry,description,reason_relevant,source_urls_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)", (name.strip(), nn, domain, website, industry, description, reason, jdump(source_urls or []), now_iso()))
            return cid, True
        except sqlite3.IntegrityError:
            ex = self.find_company(domain, name)
            return ex["id"], False

    def get_company(self, cid):
        r = self.db.row("SELECT * FROM companies WHERE id=?", (cid,))
        if r:
            r["source_urls"] = jload(r.pop("source_urls_json"), [])
        return r

    # ---- leads -------------------------------------------------------------
    def ensure_lead(self, event_id, company_id):
        camp = self.campaign_for_event(event_id)
        ex = self.db.row("SELECT id FROM leads WHERE event_id=? AND company_id=?", (event_id, company_id))
        if ex:
            return ex["id"], False
        lid = self.db.run("INSERT INTO leads(event_id,company_id,campaign_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                          (event_id, company_id, camp["id"], now_iso(), now_iso()))
        return lid, True

    def get_lead(self, lid):
        r = self.db.row("SELECT l.*, c.name company_name, c.domain, c.website, c.industry, c.description, c.reason_relevant "
                        "FROM leads l JOIN companies c ON c.id=l.company_id WHERE l.id=?", (lid,))
        if r:
            r["score"] = jload(r.get("score_json"), None)
        return r

    def list_leads(self, event_id, order="fit"):
        rows = self.db.rows(
            "SELECT l.*, c.name company_name, c.domain, c.industry, ct.email contact_email, ct.name contact_name, ct.department contact_dept "
            "FROM leads l JOIN companies c ON c.id=l.company_id LEFT JOIN contacts ct ON ct.id=l.primary_contact_id "
            "WHERE l.event_id=? ORDER BY COALESCE(l.fit_score,-1) DESC, c.name", (event_id,))
        for r in rows:
            r["score"] = jload(r.get("score_json"), None)
        return rows

    def update_lead(self, lid, **cols):
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.run(f"UPDATE leads SET {sets} WHERE id=?", (*cols.values(), lid))

    # ---- contacts ----------------------------------------------------------
    def upsert_contact(self, company_id, c: dict):
        ne = normalize_email(c["email"])
        if not ne or not is_valid_email(c["email"].strip().lower()):
            raise ValueError("invalid email")
        ex = self.db.row("SELECT id FROM contacts WHERE company_id=? AND normalized_email=?", (company_id, ne))
        if ex:
            self.db.run("UPDATE contacts SET name=COALESCE(?,name), role=COALESCE(?,role), department=?, source_url=?, confidence=?, priority=? WHERE id=?",
                        (c.get("name"), c.get("role"), c.get("department"), c["source_url"], c["confidence"], c.get("priority", 9), ex["id"]))
            return ex["id"]
        return self.db.run("INSERT INTO contacts(company_id,name,role,department,email,normalized_email,source_url,confidence,priority,created_at)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?)", (company_id, c.get("name"), c.get("role"), c.get("department"), c["email"].strip().lower(),
                                                           ne, c["source_url"], c["confidence"], c.get("priority", 9), now_iso()))

    def contacts_for(self, company_id):
        return self.db.rows("SELECT * FROM contacts WHERE company_id=? ORDER BY priority, confidence DESC", (company_id,))

    def get_contact(self, cid):
        return self.db.row("SELECT * FROM contacts WHERE id=?", (cid,)) if cid else None

    # ---- research ----------------------------------------------------------
    def get_research(self, company_id, version):
        r = self.db.row("SELECT * FROM company_research WHERE company_id=? AND research_version=?", (company_id, version))
        if not r:
            return None
        return {"status": r["status"], "profile": jload(r["profile_json"], {}), "facts": jload(r["facts_json"], []),
                "stats": jload(r["stats_json"], {}), "researched_at": r["researched_at"], "error": r["error"]}

    def sources_for(self, company_id):
        return self.db.rows("SELECT * FROM research_sources WHERE company_id=? ORDER BY id", (company_id,))

    # ---- drafts ------------------------------------------------------------
    def get_draft(self, did):
        r = self.db.row("SELECT * FROM email_drafts WHERE id=?", (did,))
        return self._draft(r)

    @staticmethod
    def _draft(r):
        if not r:
            return None
        for k in ("personalization", "evidence", "quality"):
            r[k] = jload(r.pop(f"{k}_json", None), [] if k != "quality" else {})
        return r

    def latest_draft_for_lead(self, lid):
        return self._draft(self.db.row("SELECT * FROM email_drafts WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lid,)))

    def set_draft_status(self, did, status, lead_id=None, **cols):
        cols["status"] = status
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.run(f"UPDATE email_drafts SET {sets} WHERE id=?", (*cols.values(), did))
        lid = lead_id or self.db.scalar("SELECT lead_id FROM email_drafts WHERE id=?", (did,))
        self.update_lead(lid, email_status=status)

    # ---- stats -------------------------------------------------------------
    def stats(self, event_id=None, high_fit=70):
        w, p = ("WHERE event_id=?", (event_id,)) if event_id else ("", ())
        leads = self.db.scalar(f"SELECT COUNT(*) FROM leads {w}", p)
        high = self.db.scalar(f"SELECT COUNT(*) FROM leads {w + (' AND' if w else 'WHERE')} fit_score>=?", (*p, high_fit))
        researched = self.db.scalar(f"SELECT COUNT(*) FROM leads {w + (' AND' if w else 'WHERE')} research_status='COMPLETED'", p)
        dj = "JOIN leads l ON l.id=d.lead_id" + (" WHERE l.event_id=?" if event_id else "")
        drafted = self.db.scalar(f"SELECT COUNT(DISTINCT d.lead_id) FROM email_drafts d {dj}", p)
        approved = self.db.scalar(f"SELECT COUNT(*) FROM email_drafts d {dj}{' AND' if event_id else ' WHERE'} d.status='APPROVED'", p)
        sj = "JOIN campaigns c ON c.id=s.campaign_id" + (" WHERE c.event_id=?" if event_id else "")
        sent = self.db.scalar(f"SELECT COUNT(*) FROM email_sends s {sj}{' AND' if event_id else ' WHERE'} s.status IN ('SENT','REPLIED','BOUNCED')", p)
        replies = self.db.scalar(f"SELECT COUNT(*) FROM email_sends s {sj}{' AND' if event_id else ' WHERE'} s.status='REPLIED'", p)
        with_contact = self.db.scalar(f"SELECT COUNT(*) FROM leads {w + (' AND' if w else 'WHERE')} contact_status='found'", p)
        return {"companies": leads, "high_fit": high, "researched": researched, "with_contact": with_contact, "drafted": drafted,
                "approved": approved, "sent": sent, "replies": replies, "conversion": round(100 * replies / sent, 1) if sent else 0.0,
                "pending": max(leads - sent, 0)}
