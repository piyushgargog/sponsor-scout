"""Advisory LLM tasks (fit analysis, email review) and prompt generation: the model may comment, never invent evidence."""
import unittest

from app import prompts, quality
from app.llm.base import LLMError
from tests.helpers import AppCase, ScriptedLLM


def lead_with_research(case, name="NimbusForge"):
    case.run_pipeline()
    return case.lead_by_name(name)


class FitAnalysisTests(AppCase):
    def test_analysis_is_stored_next_to_an_unchanged_score(self):
        self.run_pipeline()
        for l in self.repo.list_leads(self.event_id):
            if l["fit_score"] is None:
                continue
            a = l["score"]["analysis"]
            self.assertIn(a["priority"], ("high", "medium", "low"))
            self.assertEqual(sum(v["score"] for v in l["score"]["breakdown"].values()), l["fit_score"])  # analysis did not touch the score

    def test_uncited_or_invented_strengths_are_dropped(self):
        llm = ScriptedLLM({"fit_analysis": {"summary": "s", "priority": "HIGH", "recommended_angle": "x", "concerns": ["c"],
                                            "strengths": [{"text": "Runs a huge global hackathon series", "fact_ids": []},
                                                          {"text": "Invented partnership", "fact_ids": ["F999"]},
                                                          {"text": "Real one", "fact_ids": ["F1"]}]}})
        self.svc.pipeline.llm = llm
        self.run_pipeline()
        a = next(l for l in self.repo.list_leads(self.event_id) if l["score"] and l["score"].get("analysis"))["score"]["analysis"]
        self.assertEqual([s["text"] for s in a["strengths"]], ["Real one"])
        self.assertTrue(a["strengths"][0]["sources"][0].startswith("http"))
        self.assertEqual(a["priority"], "high")
        self.assertEqual(a["dropped_uncited"], 2)

    def test_bad_priority_becomes_unknown(self):
        self.svc.pipeline.llm = ScriptedLLM({"fit_analysis": {"summary": "s", "priority": "urgent!!!", "strengths": []}})
        self.run_pipeline()
        a = next(l for l in self.repo.list_leads(self.event_id) if l["score"] and l["score"].get("analysis"))["score"]["analysis"]
        self.assertEqual(a["priority"], "unknown")

    def test_llm_failure_leaves_score_and_research_intact(self):
        self.svc.pipeline.llm = ScriptedLLM({"fit_analysis": LLMError("quota")})
        self.run_pipeline()
        scored = [l for l in self.repo.list_leads(self.event_id) if l["fit_score"] is not None]
        self.assertTrue(scored)
        self.assertTrue(all("analysis" not in l["score"] for l in scored))
        self.assertTrue(all(l["research_status"] == "COMPLETED" for l in scored))

    def test_can_be_disabled(self):
        self.settings.llm_fit_analysis = False
        llm = ScriptedLLM()
        self.svc.pipeline.llm = llm
        self.run_pipeline()
        self.assertNotIn("fit_analysis", llm.calls)

    def test_prompt_contains_only_given_facts_and_forbids_rescoring(self):
        ev, co = self.repo.get_event(self.event_id), {"name": "Acme", "industry": "cloud"}
        score = {"score": 55, "breakdown": {"x": {"label": "Student Audience Fit", "score": 8, "max": 20}}}
        p = prompts.fit_analysis_prompt(ev, co, score, [{"id": "F1", "text": "Acme runs a student program.", "source_url": "https://acme.example/students"}])
        self.assertIn("F1: Acme runs a student program.", p)
        self.assertIn("55/100", p)
        self.assertIn("do NOT change or re-score", p)
        self.assertIn("discarded", p)


class EmailReviewTests(AppCase):
    def draft_for(self, llm):
        self.svc.pipeline.llm = llm
        self.run_pipeline()
        lead = next(l for l in self.repo.list_leads(self.event_id) if l["contact_status"] == "found" and l["research_status"] == "COMPLETED")
        did = self.pipe.generate_email(lead["id"])
        return self.repo.get_draft(did)

    def test_flagged_sentence_present_in_body_adds_warning(self):
        def review(ctx):
            first = ctx["body"].split("\n")[2]
            return {"unsupported_claims": [{"sentence": first, "reason": "not in facts"}], "weak_personalization": False}
        d = self.draft_for(ScriptedLLM({"email_review": review}))
        c = next(c for c in d["quality"]["checks"] if c["name"] == "ai_unsupported_claims")
        self.assertEqual(c["status"], "warn")
        self.assertEqual(d["status"], "NEEDS_REVIEW")

    def test_invented_flag_for_text_not_in_email_is_ignored(self):
        d = self.draft_for(ScriptedLLM({"email_review": {"unsupported_claims": [{"sentence": "This sentence never appears.", "reason": "x"}]}}))
        self.assertFalse([c for c in d["quality"]["checks"] if c["name"] == "ai_unsupported_claims"])

    def test_weak_personalization_flag(self):
        d = self.draft_for(ScriptedLLM({"email_review": {"unsupported_claims": [], "weak_personalization": True}}))
        self.assertTrue([c for c in d["quality"]["checks"] if c["name"] == "ai_weak_personalization"])

    def test_review_failure_never_blocks_drafting(self):
        d = self.draft_for(ScriptedLLM({"email_review": LLMError("down")}))
        self.assertIn(d["status"], ("DRAFT", "NEEDS_REVIEW"))
        self.assertFalse([c for c in d["quality"]["checks"] if c["name"].startswith("ai_")])

    def test_review_can_only_warn_never_approve_a_blocked_draft(self):
        # a draft with no contact email stays blocked even if the model says everything is fine
        q = quality.evaluate({"subject": "s", "body": "Hi\n\nshort", "personalization": []}, {"name": "E", "event_date": "2026-01-01"},
                             {"name": "Co"}, None, [], 50, extra_checks=[{"name": "ai_review", "status": "pass", "detail": "fine"}])
        self.assertEqual(q["status"], "blocked")

    def test_ai_warning_follows_the_sentence_through_edits(self):
        prev = {"checks": [{"name": "ai_unsupported_claims", "status": "warn", "sentences": ["We love your hackathon."], "detail": "d"},
                           {"name": "length", "status": "warn", "detail": "n/a"}]}
        self.assertEqual(len(quality.carry_llm_checks(prev, "Hi\n\nWe love your hackathon.\n\n-- \nName")), 1)
        self.assertEqual(quality.carry_llm_checks(prev, "Hi\n\nRewritten sentence.\n\n-- \nName"), [])

    def test_ai_warning_survives_requality_and_requires_acknowledgement(self):
        def review(ctx):
            return {"unsupported_claims": [{"sentence": ctx["body"].split("\n")[2], "reason": "r"}]}
        d = self.draft_for(ScriptedLLM({"email_review": review}))
        from app.sending import ApprovalError
        with self.assertRaises(ApprovalError):
            self.sender.approve(d["id"], "tester")                    # warning not acknowledged
        self.sender.approve(d["id"], "tester", acknowledge=True)
        self.assertEqual(self.repo.get_draft(d["id"])["status"], "APPROVED")

    def test_review_prompt_carries_facts_and_email(self):
        p = prompts.email_review_prompt({"name": "E"}, "Hello body", [{"id": "F2", "text": "Fact two."}])
        self.assertIn("F2: Fact two.", p)
        self.assertIn("Hello body", p)
        self.assertIn("EXACTLY", p)

    def test_can_be_disabled(self):
        self.settings.llm_email_review = False
        llm = ScriptedLLM()
        self.draft_for(llm)
        self.assertNotIn("email_review", llm.calls)


if __name__ == "__main__":
    unittest.main()
