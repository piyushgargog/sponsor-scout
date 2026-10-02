"""Human-approval gate, send guards (rate limit, duplicates, cooldown) and status tracking."""
import sqlite3
from datetime import datetime, timezone

from .jobs import Defer, PermanentError
from .logging_setup import log_event, redact
from .mailer.base import MailAuthError, MailError
from .normalize import is_placeholder_email, normalize_email
from .util import iso_ago, jdump, now_iso

LIVE = ("SENDING", "SENT", "REPLIED", "BOUNCED")


class ApprovalError(Exception):
    pass


class SendBlocked(Exception):
    pass


class DuplicateSendError(SendBlocked):
    pass


class Sender:
    def __init__(self, repo, settings, mailer, accounts, queue, pipeline):
        self.repo, self.db, self.s = repo, repo.db, settings
        self.mailer, self.accounts, self.queue, self.pipeline = mailer, accounts, queue, pipeline

    # ---- editing & approval ---------------------------------------------
    def update_draft(self, draft_id, subject, body, actor):
        d = self.repo.get_draft(draft_id)
        if not d or d["status"] in ("SENT", "REPLIED", "BOUNCED"):
            raise ApprovalError("This draft can no longer be edited.")
        subject = " ".join((subject or "").split())[:150]
        body = (body or "").replace("\r\n", "\n").strip()
        if not subject or not body or len(body) > 6000:
            raise ApprovalError("Subject and body are required (body max 6000 characters).")
        self.db.run("UPDATE email_drafts SET subject=?, body=? WHERE id=?", (subject, body, draft_id))
        q = self.pipeline.requality(draft_id)
        status = "DRAFT" if q["status"] == "ok" else "NEEDS_REVIEW"
        self.db.run("UPDATE email_drafts SET quality_json=?, approved_by=NULL, approved_at=NULL WHERE id=?",
                    (jdump(q), draft_id))
        self.repo.set_draft_status(draft_id, status)
        self.repo.audit(actor, "draft_edited", "draft", draft_id, quality=q["status"])
        return q

    def approve(self, draft_id, actor, acknowledge=False):
        d = self.repo.get_draft(draft_id)
        if not d:
            raise ApprovalError("Draft not found.")
        if d["status"] not in ("DRAFT", "NEEDS_REVIEW"):
            raise ApprovalError(f"A draft in status {d['status']} cannot be approved.")
        q = self.pipeline.requality(draft_id)
        self.db.run("UPDATE email_drafts SET quality_json=? WHERE id=?", (jdump(q), draft_id))
        if q["status"] == "blocked":
            raise ApprovalError("Blocked by quality checks: " + "; ".join(c["detail"] for c in q["checks"] if c["status"] == "block"))
        if q["status"] == "needs_review" and not acknowledge:
            raise ApprovalError("This draft has warnings. Review them and tick 'I have reviewed the warnings' to approve.")
        self.repo.set_draft_status(draft_id, "APPROVED", approved_by=actor, approved_at=now_iso())
        self.repo.audit(actor, "email_approved", "draft", draft_id, acknowledged_warnings=bool(acknowledge))
        log_event("email_approved", draft_id=draft_id, actor=actor)

    def unapprove(self, draft_id, actor):
        d = self.repo.get_draft(draft_id)
        if d and d["status"] == "APPROVED":
            self.repo.set_draft_status(draft_id, "DRAFT", approved_by=None, approved_at=None)
            self.repo.audit(actor, "approval_revoked", "draft", draft_id)

    # ---- sending -----------------------------------------------------------
    def request_send(self, draft_id, actor) -> int:
        d = self.repo.get_draft(draft_id)
        if not d or d["status"] != "APPROVED" or not d.get("approved_by"):
            raise SendBlocked("Only drafts approved by a human can be sent.")
        if not self.accounts.get_active(self.mailer.name):
            raise SendBlocked("Connect a sending account first.")
        lead = self.repo.get_lead(d["lead_id"])
        self.repo.audit(actor, "send_requested", "draft", draft_id)
        return self.queue.enqueue("send_email", {"draft_id": draft_id, "actor": actor}, dedupe_key=f"send:{draft_id}",
                                  event_id=lead["event_id"], label=f"Send to {lead['company_name']}", max_attempts=1)

    def send_now(self, draft_id, actor="system") -> dict:
        d = self.repo.get_draft(draft_id)
        if not d or d["status"] != "APPROVED" or not d.get("approved_by"):
            raise PermanentError("Draft is not approved.")
        lead = self.repo.get_lead(d["lead_id"])
        contact = self.repo.get_contact(d["contact_id"])
        camp = self.db.row("SELECT * FROM campaigns WHERE id=?", (lead["campaign_id"],))
        if camp["status"] != "ACTIVE":
            raise PermanentError("Campaign is paused.")
        to = (contact or {}).get("email")
        ne = normalize_email(to)
        if not ne:
            self._fail(d, "No valid recipient email.")
            raise PermanentError("No valid recipient email.")
        if self.s.mail_provider == "gmail" and is_placeholder_email(to):
            self._fail(d, "Recipient domain is a placeholder.")
            raise PermanentError("Recipient domain is a placeholder.")
        q = self.pipeline.requality(draft_id)
        if q["status"] == "blocked":
            self._fail(d, "Blocked by quality checks at send time.")
            raise PermanentError("Blocked by quality checks at send time.")
        account = self.accounts.get_active(self.mailer.name)
        if not account:
            raise PermanentError("No connected sending account.")

        # campaign-level daily cap
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        n_today = self.db.scalar("SELECT COUNT(*) FROM email_sends WHERE campaign_id=? AND created_at>=? AND status IN ('SENDING','SENT','REPLIED','BOUNCED')",
                                 (camp["id"], today))
        if n_today >= camp["daily_limit"]:
            raise PermanentError(f"Daily send limit reached for this campaign ({camp['daily_limit']}). Try again tomorrow.")
        # throttle between sends
        if self.s.send_interval_seconds:
            last = self.db.scalar("SELECT MAX(sent_at) FROM email_sends WHERE sender_account=? AND status IN ('SENT','REPLIED','BOUNCED')", (account["email"],))
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
                if elapsed < self.s.send_interval_seconds:
                    raise Defer(int(self.s.send_interval_seconds - elapsed) + 1, "send throttle")
        # company cooldown across campaigns
        recent = self.db.row("SELECT 1 FROM email_sends WHERE company_id=? AND campaign_id<>? AND status IN ('SENT','REPLIED','BOUNCED') AND created_at>=?",
                             (lead["company_id"], camp["id"], iso_ago(days=self.s.company_cooldown_days)))
        if recent:
            self._fail(d, f"Company was already contacted in the last {self.s.company_cooldown_days} days.")
            raise PermanentError("Duplicate protection: company contacted recently by another campaign.")

        try:
            sid = self.db.run("INSERT INTO email_sends(draft_id,campaign_id,company_id,contact_id,to_email,normalized_email,status,sender_account,created_at)"
                              " VALUES(?,?,?,?,?,?,'SENDING',?,?)", (draft_id, camp["id"], lead["company_id"], contact["id"], to, ne, account["email"], now_iso()))
        except sqlite3.IntegrityError:
            same = self.db.row("SELECT 1 FROM email_sends WHERE draft_id=? AND status IN ('SENDING','SENT','REPLIED','BOUNCED')", (draft_id,))
            if same:
                return {"skipped": "already sent or in progress"}
            self._fail(d, "Duplicate protection: this mailbox or company was already contacted.")
            raise PermanentError("Duplicate protection: this mailbox or company was already contacted.")

        try:
            res = self.mailer.send(account, to, d["subject"], d["body"], self.s.sender_name)
        except MailAuthError as e:
            self._send_failed(sid, d, f"Auth: {e}")
            raise PermanentError(f"Gmail authorization problem: {e}")
        except MailError as e:
            self._send_failed(sid, d, str(e))
            raise PermanentError(str(e))
        except Exception as e:  # noqa: BLE001
            self._send_failed(sid, d, f"{type(e).__name__}")
            raise
        self.db.run("UPDATE email_sends SET status='SENT', gmail_message_id=?, thread_id=?, sent_at=? WHERE id=?", (res.message_id, res.thread_id, now_iso(), sid))
        self.repo.set_draft_status(draft_id, "SENT", lead["id"])
        self.repo.audit(actor, "email_sent", "draft", draft_id, to=to, message_id=res.message_id)
        log_event("email_sent", draft_id=draft_id, lead_id=lead["id"], message_id=res.message_id, provider=self.mailer.name)
        return {"message_id": res.message_id}

    def _send_failed(self, sid, d, msg):
        msg = redact(msg)[:300]
        self.db.run("UPDATE email_sends SET status='FAILED', error=? WHERE id=?", (msg, sid))
        self._fail(d, msg)

    def _fail(self, d, msg):
        self.repo.set_draft_status(d["id"], "FAILED", error=msg[:300])
        self.repo.audit("system", "email_failed", "draft", d["id"], error=msg[:200])
        log_event("email_failed", draft_id=d["id"], error=msg[:200])

    def retry_failed(self, draft_id, actor):
        d = self.repo.get_draft(draft_id)
        if d and d["status"] == "FAILED":
            self.repo.set_draft_status(draft_id, "NEEDS_REVIEW", error=None, approved_by=None, approved_at=None)
            self.repo.audit(actor, "draft_reopened", "draft", draft_id)

    # ---- outcome tracking (manual: Gmail reply detection needs a restricted read scope) ----
    def mark_outcome(self, draft_id, outcome, actor):
        if outcome not in ("REPLIED", "BOUNCED"):
            raise ApprovalError("Invalid outcome.")
        d = self.repo.get_draft(draft_id)
        if not d or d["status"] not in ("SENT", "REPLIED", "BOUNCED"):
            raise ApprovalError("Only sent emails can be marked.")
        self.db.run("UPDATE email_sends SET status=? WHERE draft_id=? AND status IN ('SENT','REPLIED','BOUNCED')", (outcome, draft_id))
        self.repo.set_draft_status(draft_id, outcome)
        self.repo.audit(actor, f"email_marked_{outcome.lower()}", "draft", draft_id)
