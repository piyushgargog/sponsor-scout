import sqlite3
import unittest
from unittest import mock

from app.jobs import Defer, PermanentError
from app.mailer.base import MailAuthError, MailError
from app.sending import ApprovalError, SendBlocked
from app.util import iso_ago
from tests.helpers import AppCase, EVENT


class SendFlowBase(AppCase):
    def setUp(self):
        super().setUp()
        self.run_pipeline()
        self.connect_demo()


class ApprovalGateTests(SendFlowBase):
    def test_nothing_sends_without_approval(self):
        lead = self.lead_by_name("NimbusForge Cloud")
        did = self.pipe.generate_email(lead["id"])
        self.assertIn(self.repo.get_draft(did)["status"], ("DRAFT", "NEEDS_REVIEW"))
        with self.assertRaises(SendBlocked):
            self.sender.request_send(did, "t")
        with self.assertRaises(PermanentError):
            self.sender.send_now(did, "t")
        self.assertEqual(self.svc.mailer.outbox, [])

    def test_approve_then_send_records_everything(self):
        did, lead = self.approved_draft("NimbusForge Cloud")
        d = self.repo.get_draft(did)
        self.assertEqual((d["status"], d["approved_by"]), ("APPROVED", "tester"))
        self.sender.request_send(did, "tester")
        self.svc.queue.run_all()
        d = self.repo.get_draft(did)
        self.assertEqual(d["status"], "SENT")
        send = self.db.row("SELECT * FROM email_sends WHERE draft_id=?", (did,))
        self.assertEqual(send["status"], "SENT")
        self.assertTrue(send["gmail_message_id"].startswith("mock-"))
        self.assertEqual(send["sender_account"], "demo@mock.local")
        self.assertEqual(self.repo.get_lead(lead["id"])["email_status"], "SENT")
        self.assertEqual(len(self.svc.mailer.outbox), 1)
        self.assertEqual(self.svc.mailer.outbox[0]["to"], send["to_email"])
        actions = [a["action"] for a in self.repo.audit_for("draft", did)]
        for a in ("email_generated", "email_approved", "send_requested", "email_sent"):
            self.assertIn(a, actions)

    def test_editing_resets_approval(self):
        did, lead = self.approved_draft("NimbusForge Cloud")
        d = self.repo.get_draft(did)
        self.sender.update_draft(did, d["subject"], d["body"] + "\n\nP.S. one more thing.", "tester")
        d = self.repo.get_draft(did)
        self.assertNotEqual(d["status"], "APPROVED")
        self.assertIsNone(d["approved_by"])
        with self.assertRaises(SendBlocked):
            self.sender.request_send(did, "t")

    def test_blocked_draft_cannot_be_approved_and_warnings_need_ack(self):
        lead = self.lead_by_name("NimbusForge Cloud")
        did = self.pipe.generate_email(lead["id"])
        d = self.repo.get_draft(did)
        self.sender.update_draft(did, "URGENT!!! ACT NOW", "Hi team,\n\nLimited time guaranteed offer!!! Click here https://a.example https://b.example", "t")
        with self.assertRaises(ApprovalError):  # short + no evidence in body -> blocked
            self.sender.approve(did, "t", acknowledge=True)
        did2 = self.pipe.generate_email(lead["id"])  # regenerate in place
        self.sender.update_draft(did2, d["subject"], d["body"].replace("Would you be open", "Act now, limited time! Would you be open"), "t")
        self.assertEqual(self.repo.get_draft(did2)["status"], "NEEDS_REVIEW")
        with self.assertRaises(ApprovalError):
            self.sender.approve(did2, "t", acknowledge=False)
        self.sender.approve(did2, "t", acknowledge=True)
        self.assertEqual(self.repo.get_draft(did2)["status"], "APPROVED")

    def test_cannot_regenerate_after_send(self):
        did, lead = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did, "t")
        with self.assertRaises(PermanentError):
            self.pipe.generate_email(lead["id"])


class DuplicateProtectionTests(SendFlowBase):
    def test_same_mailbox_cannot_be_emailed_twice(self):
        did1, l1 = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did1, "t")
        mailbox = self.repo.get_contact(self.repo.get_draft(did1)["contact_id"])["email"]
        l2 = self.lead_by_name("QueryDock")
        self.pipe.add_manual_contact(l2["id"], mailbox.upper(), None, None, "t")  # same mailbox, different case, other company
        did2 = self.pipe.generate_email(l2["id"])
        d2 = self.repo.get_draft(did2)
        self.sender.approve(did2, "t", acknowledge=d2["status"] == "NEEDS_REVIEW")
        with self.assertRaises(PermanentError):
            self.sender.send_now(did2, "t")
        self.assertEqual(self.repo.get_draft(did2)["status"], "FAILED")
        self.assertEqual(len(self.svc.mailer.outbox), 1)

    def test_plus_tag_variant_is_same_mailbox(self):
        did1, _ = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did1, "t")
        mailbox = self.repo.get_contact(self.repo.get_draft(did1)["contact_id"])["email"]
        local, dom = mailbox.split("@")
        l2 = self.lead_by_name("QueryDock")
        self.pipe.add_manual_contact(l2["id"], f"{local}+spring@{dom}", None, None, "t")
        did2 = self.pipe.generate_email(l2["id"])
        d2 = self.repo.get_draft(did2)
        self.sender.approve(did2, "t", acknowledge=d2["status"] == "NEEDS_REVIEW")
        with self.assertRaises(PermanentError):
            self.sender.send_now(did2, "t")

    def test_one_live_send_per_company_per_campaign(self):
        did, lead = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did, "t")
        send = self.db.row("SELECT * FROM email_sends")
        with self.assertRaises(sqlite3.IntegrityError):  # DB-level guarantee, even if app logic is bypassed
            self.db.run("INSERT INTO email_sends(draft_id,campaign_id,company_id,to_email,normalized_email,status,created_at) VALUES(?,?,?,?,?,'SENT','t')",
                        (did, send["campaign_id"], send["company_id"], "other@x.example", "other@x.example"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.run("INSERT INTO email_sends(draft_id,campaign_id,company_id,to_email,normalized_email,status,created_at) VALUES(?,?,?,?,?,'SENDING','t')",
                        (did, 999, 999, send["to_email"], send["normalized_email"]))

    def test_double_click_does_not_double_send(self):
        did, _ = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did, "t")
        self.db.run("UPDATE email_drafts SET status='APPROVED' WHERE id=?", (did,))  # simulate a racing second job
        out = self.sender.send_now(did, "t")
        self.assertIn("skipped", out)
        self.assertEqual(len(self.svc.mailer.outbox), 1)

    def test_company_cooldown_across_campaigns(self):
        did, lead = self.approved_draft("NimbusForge Cloud")
        self.sender.send_now(did, "t")
        ev2 = self.repo.create_event(dict(EVENT, name="Other Fest"), 20)
        lid2, _ = self.repo.ensure_lead(ev2, lead["company_id"])
        self.pipe.research_lead(lid2)
        self.db.run("UPDATE contacts SET normalized_email='fresh@nimbusforge.example', email='fresh@nimbusforge.example' WHERE id=?", (self.repo.get_lead(lid2)["primary_contact_id"],))
        did2 = self.pipe.generate_email(lid2)
        d2 = self.repo.get_draft(did2)
        self.sender.approve(did2, "t", acknowledge=d2["status"] == "NEEDS_REVIEW")
        with self.assertRaises(PermanentError) as cm:
            self.sender.send_now(did2, "t")
        self.assertIn("Duplicate protection", str(cm.exception))


class RateLimitAndFailureTests(SendFlowBase):
    def test_daily_cap(self):
        self.db.run("UPDATE campaigns SET daily_limit=1")
        d1, _ = self.approved_draft("NimbusForge Cloud")
        d2, _ = self.approved_draft("Tensorloom AI")
        self.sender.send_now(d1, "t")
        with self.assertRaises(PermanentError) as cm:
            self.sender.send_now(d2, "t")
        self.assertIn("Daily send limit", str(cm.exception))
        self.assertEqual(self.repo.get_draft(d2)["status"], "APPROVED")  # still approved, can send tomorrow

    def test_throttle_defers_instead_of_failing(self):
        self.settings.send_interval_seconds = 60
        d1, _ = self.approved_draft("NimbusForge Cloud")
        d2, _ = self.approved_draft("Tensorloom AI")
        self.sender.send_now(d1, "t")
        with self.assertRaises(Defer) as cm:
            self.sender.send_now(d2, "t")
        self.assertGreater(cm.exception.seconds, 0)
        self.db.run("UPDATE email_sends SET sent_at=?", (iso_ago(seconds=120),))
        self.sender.send_now(d2, "t")
        self.assertEqual(self.repo.get_draft(d2)["status"], "SENT")

    def test_paused_campaign_blocks_sending(self):
        d1, _ = self.approved_draft("NimbusForge Cloud")
        self.db.run("UPDATE campaigns SET status='PAUSED'")
        with self.assertRaises(PermanentError):
            self.sender.send_now(d1, "t")

    def test_provider_failure_marks_failed_and_frees_the_slot(self):
        d1, _ = self.approved_draft("NimbusForge Cloud")
        with mock.patch.object(self.svc.mailer, "send", side_effect=MailError("boom")):
            with self.assertRaises(PermanentError):
                self.sender.send_now(d1, "t")
        d = self.repo.get_draft(d1)
        self.assertEqual(d["status"], "FAILED")
        self.assertIn("boom", d["error"])
        self.assertEqual(self.db.row("SELECT status FROM email_sends")["status"], "FAILED")
        self.sender.retry_failed(d1, "t")  # human reopens; slot is free again
        self.sender.approve(d1, "t", acknowledge=True)
        self.sender.send_now(d1, "t")
        self.assertEqual(self.repo.get_draft(d1)["status"], "SENT")

    def test_auth_failure_is_reported(self):
        d1, _ = self.approved_draft("NimbusForge Cloud")
        with mock.patch.object(self.svc.mailer, "send", side_effect=MailAuthError("revoked")):
            with self.assertRaises(PermanentError) as cm:
                self.sender.send_now(d1, "t")
        self.assertIn("authorization", str(cm.exception).lower())

    def test_placeholder_recipient_blocked_when_gmail_is_the_provider(self):
        d1, _ = self.approved_draft("NimbusForge Cloud")
        self.settings.mail_provider = "gmail"
        with self.assertRaises(PermanentError):
            self.sender.send_now(d1, "t")
        self.assertEqual(self.svc.mailer.outbox, [])


class OutcomeTrackingTests(SendFlowBase):
    def test_replied_and_conversion(self):
        d1, lead = self.approved_draft("NimbusForge Cloud")
        with self.assertRaises(ApprovalError):
            self.sender.mark_outcome(d1, "REPLIED", "t")  # not sent yet
        self.sender.send_now(d1, "t")
        self.sender.mark_outcome(d1, "REPLIED", "t")
        st = self.repo.stats(self.event_id)
        self.assertEqual((st["sent"], st["replies"], st["conversion"]), (1, 1, 100.0))
        self.assertEqual(self.repo.get_lead(lead["id"])["email_status"], "REPLIED")
        self.sender.mark_outcome(d1, "BOUNCED", "t")
        self.assertEqual(self.repo.get_draft(d1)["status"], "BOUNCED")


if __name__ == "__main__":
    unittest.main()
