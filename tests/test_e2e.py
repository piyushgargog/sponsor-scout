"""The 'Definition of Done' walkthrough, driven through the real HTTP routes in demo/mock mode."""
import unittest

from tests.test_web import EVENT_FORM, WebCase


class DefinitionOfDoneTests(WebCase):
    def test_full_flow(self):
        self.assertEqual(self.login().status_code, 302)                                    # 1. start application / log in
        r = self.post("/events", **EVENT_FORM)                                              # 2. create event (+ 3. "Find Sponsors" runs automatically)
        self.assertEqual(r.headers["Location"], "/campaigns/1")
        svc = self.svc
        self.assertEqual(svc.queue.summary(1)["QUEUED"], 1)
        svc.queue.run_all()
        self.assertEqual(svc.queue.summary(1)["FAILED"], 0)
        leads = svc.repo.list_leads(1)
        self.assertEqual(len(leads), 9)                                                     # 4. companies discovered (no hardcoded list in logic)
        self.assertTrue(all(l["fit_score"] is not None for l in leads))                     # 5. fit scores
        self.assertTrue(all(l["research_status"] == "COMPLETED" for l in leads))           # 6. researched
        top = leads[0]
        page = self.c.get(f"/leads/{top['id']}").get_data(as_text=True)
        self.assertIn("https://www.", page)                                                 # 7. sources displayed
        self.assertIn("Why it fits", page)
        self.assertEqual(sum(l["contact_status"] == "found" for l in leads), 8)             # 8. contacts discovered where public
        self.assertEqual([l for l in leads if l["contact_status"] == "not_found"][0]["company_name"], "DeployDen Hosting")
        self.post("/gmail/demo-connect")
        self.post(f"/leads/{top['id']}/generate", page=f"/leads/{top['id']}")               # 9. personalized email
        svc.queue.run_all()
        d = svc.repo.latest_draft_for_lead(top["id"])
        self.assertIn(d["status"], ("DRAFT", "NEEDS_REVIEW"))
        self.assertTrue(d["evidence"])
        detail = self.c.get(f"/leads/{top['id']}").get_data(as_text=True)
        self.assertIn("Why each claim was written", detail)
        new_subject = "Edited: partnership with XYZ Tech Fest"                              # 10. review / edit
        self.post(f"/drafts/{d['id']}/save", page=f"/leads/{top['id']}", subject=new_subject, body=d["body"])
        d = svc.repo.get_draft(d["id"])
        self.assertEqual(d["subject"], new_subject)
        ack = {"acknowledge": "1"} if d["status"] == "NEEDS_REVIEW" else {}
        self.post(f"/drafts/{d['id']}/approve", page=f"/leads/{top['id']}", **ack)         # 11. approve
        self.assertEqual(svc.repo.get_draft(d["id"])["status"], "APPROVED")
        self.post(f"/drafts/{d['id']}/send", page=f"/leads/{top['id']}")                    # 12. send
        svc.queue.run_all()
        self.assertEqual(svc.repo.get_draft(d["id"])["status"], "SENT")
        self.assertEqual(len(svc.mailer.outbox), 1)
        self.assertEqual(svc.mailer.outbox[0]["subject"], new_subject)
        st = svc.repo.stats(1)                                                              # 13. dashboard records it
        self.assertEqual(st["sent"], 1)
        self.assertIn("Emails sent", self.c.get("/").get_data(as_text=True))
        self.assertRegex(self.c.get("/campaigns/1").get_data(as_text=True), r"<b>1</b><span>Sent</span>")
        # 14. duplicate protection: re-running discovery adds no leads; same mailbox cannot be sent twice
        self.post("/campaigns/1/find-sponsors", page="/campaigns/1")
        svc.queue.run_all()
        self.assertEqual(len(svc.repo.list_leads(1)), 9)
        self.assertEqual(svc.db.scalar("SELECT COUNT(*) FROM companies"), 9)
        self.post(f"/drafts/{d['id']}/send", page=f"/leads/{top['id']}")
        svc.queue.run_all()
        self.assertEqual(len(svc.mailer.outbox), 1)
        self.post(f"/drafts/{d['id']}/mark", page=f"/leads/{top['id']}", outcome="REPLIED")
        self.assertEqual(svc.repo.stats(1)["conversion"], 100.0)

    def test_every_page_renders_in_every_state(self):
        self.login()
        self.assertEqual(self.c.get("/").status_code, 200)       # empty state
        self.assertEqual(self.c.get("/leads").status_code, 302)
        self.post("/events", **EVENT_FORM)
        for p in ("/", "/campaigns/1", "/leads?event=1"):         # queued/before work
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.svc.queue.run_all()
        self.post("/gmail/demo-connect")
        self.post("/campaigns/1/bulk", page="/campaigns/1", action="generate_high_fit")
        self.svc.queue.run_all()
        for l in self.svc.repo.list_leads(1):
            self.assertEqual(self.c.get(f"/leads/{l['id']}").status_code, 200, l["company_name"])
        for p in ("/", "/campaigns/1", "/leads?event=1", "/leads?event=1&filter=high", "/leads?event=1&q=cloud", "/api/jobs?event=1"):
            self.assertEqual(self.c.get(p).status_code, 200, p)

    def test_bulk_actions(self):
        self.login()
        self.post("/events", **EVENT_FORM)
        self.svc.queue.run_all()
        self.post("/gmail/demo-connect")
        self.post("/campaigns/1/bulk", page="/campaigns/1", action="generate_high_fit")
        self.svc.queue.run_all()
        drafts = self.svc.db.rows("SELECT * FROM email_drafts")
        self.assertGreaterEqual(len(drafts), 2)
        self.post("/campaigns/1/bulk", page="/campaigns/1", action="send_approved")  # nothing approved -> nothing sent
        self.svc.queue.run_all()
        self.assertEqual(self.svc.mailer.outbox, [])
        for d in drafts:
            self.post(f"/drafts/{d['id']}/approve", acknowledge="1")
        self.post("/campaigns/1/bulk", page="/campaigns/1", action="send_approved")
        self.svc.queue.run_all()
        self.assertEqual(len(self.svc.mailer.outbox), len([d for d in self.svc.repo.list_leads(1) if d["email_status"] == "SENT"]))
        self.assertGreaterEqual(len(self.svc.mailer.outbox), 2)


if __name__ == "__main__":
    unittest.main()
