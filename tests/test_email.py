import unittest

from app import quality
from app.emailgen import EmailGenError, core_body, event_context, finalize_draft, generate_draft, select_facts, suggest_ask
from app.llm.mock import MockProvider
from app.prompts import email_prompt
from app.research.extract import extract_facts
from app.research.fetcher import FixtureFetcher, crawl_company
from tests.helpers import EVENT, ScriptedLLM

SENDER = {"name": "Prateek", "role": "Sponsorship Lead", "org": "XYZ Tech Club"}
CO = {"name": "NimbusForge Cloud", "industry": "Cloud platform"}
CONTACT = {"name": None, "role": None, "email": "partnerships@nimbusforge.example", "confidence": 0.9}


def facts():
    return extract_facts(crawl_company(FixtureFetcher(), "https://www.nimbusforge.example/"))


class PromptTests(unittest.TestCase):
    def test_prompt_contains_evidence_and_guardrails(self):
        ch = select_facts(facts())
        p = email_prompt(event_context(EVENT), CO, CONTACT, ch, SENDER, EVENT["requirements"])
        for f in ch:
            self.assertIn(f["id"], p)
            self.assertIn(f["source_url"], p)
        for needle in ("XYZ Tech Fest 2026", "100-180 words", "Do not invent", "Dear Sir/Madam", "fact ids", "NimbusForge Cloud", "API credits"):
            self.assertIn(needle, p)

    def test_prompt_never_includes_secrets_or_unselected_text(self):
        p = email_prompt(event_context(EVENT), CO, CONTACT, select_facts(facts()), SENDER, EVENT["requirements"])
        self.assertNotIn("api_key", p.lower())

    def test_suggest_ask_maps_evidence_to_requirement(self):
        ch = [{"id": "F1", "categories": ["credits"], "text": "x", "source_url": "u"}]
        self.assertEqual(suggest_ask(EVENT, ch), "API credits")
        self.assertEqual(suggest_ask({"requirements": ["swag"]}, [{"id": "F1", "categories": ["hiring"], "text": "x", "source_url": "u"}]), "swag")


class GenerationTests(unittest.TestCase):
    def test_mock_draft_is_grounded_and_passes_checks(self):
        ch = select_facts(facts())
        d = generate_draft(MockProvider(), EVENT, CO, CONTACT, facts(), SENDER)
        self.assertTrue(d["subject"].startswith("Partnership opportunity"))
        self.assertIn("Prateek", d["body"].split("\n-- \n")[1])
        by_id = {f["id"]: f for f in ch}
        self.assertTrue(d["evidence"])
        for e in d["evidence"]:
            self.assertEqual(e["claim"], by_id[e["fact_id"]]["text"])
            self.assertTrue(e["source"].startswith("https://"))
            self.assertIn(e["claim"], d["body"])  # the claim really appears in the email
        q = quality.evaluate(d, event_context(EVENT), CO, CONTACT, ch, 82)
        self.assertEqual(q["status"], "ok", q["checks"])
        self.assertTrue(100 <= q["word_count"] <= 180, q["word_count"])

    def test_no_facts_refuses_to_write(self):
        with self.assertRaises(EmailGenError):
            generate_draft(MockProvider(), EVENT, CO, CONTACT, [], SENDER)
        with self.assertRaises(EmailGenError):
            generate_draft(MockProvider(), EVENT, CO, CONTACT, [{"id": "F1", "categories": ["hiring"], "text": "We are hiring.", "source_url": "u"}], SENDER)

    def test_fabricated_fact_ids_are_dropped(self):
        ch = select_facts(facts())
        out = {"subject": "Hi", "body": "Hi team,\n\nsome claim", "personalization": [{"sentence": "some claim", "fact_ids": ["F99"], "why": "x"}],
               "cta": "c", "suggested_ask": "swag"}
        d = finalize_draft(out, ch, event_context(EVENT), SENDER, "swag")
        self.assertEqual(d["personalization"], [])
        self.assertEqual(d["evidence"], [])
        self.assertEqual(d["unknown_fact_ids"], ["F99"])

    def test_llm_signature_is_replaced_not_duplicated(self):
        ch = select_facts(facts())
        out = {"subject": "S", "body": "Hi,\n\nbody\n\n-- \nFake Name", "personalization": [], "cta": "", "suggested_ask": ""}
        d = finalize_draft(out, ch, event_context(EVENT), SENDER, "swag")
        self.assertEqual(d["body"].count("\n-- \n"), 1)
        self.assertNotIn("Fake Name", d["body"])


class QualityTests(unittest.TestCase):
    def evaluate(self, body, subject="Partnership: XYZ x NimbusForge", pers=None, contact=CONTACT, fit=80, real=False):
        ch = select_facts(facts())
        d = {"subject": subject, "body": body, "personalization": pers if pers is not None else [{"sentence": ch[0]["text"], "fact_ids": [ch[0]["id"]], "why": ""}]}
        return quality.evaluate(d, event_context(EVENT), CO, contact, ch, fit, real_send=real)

    def good_body(self):
        return generate_draft(MockProvider(), EVENT, CO, CONTACT, facts(), SENDER)["body"]

    def names(self, q, status):
        return {c["name"] for c in q["checks"] if c["status"] == status}

    def test_hallucinated_evidence_is_blocked(self):
        q = self.evaluate(self.good_body(), pers=[{"sentence": "x", "fact_ids": ["F77"], "why": ""}])
        self.assertEqual(q["status"], "blocked")
        self.assertIn("evidence", self.names(q, "block"))
        q = self.evaluate(self.good_body(), pers=[])
        self.assertEqual(q["status"], "blocked")

    def test_invalid_or_missing_email_blocked(self):
        for bad in ({"email": "not-an-email"}, {"email": None}, None):
            q = self.evaluate(self.good_body(), contact=bad)
            self.assertIn("recipient", self.names(q, "block"))

    def test_placeholder_domain_blocked_only_for_real_sending(self):
        self.assertEqual(self.evaluate(self.good_body(), real=False)["status"], "ok")
        self.assertIn("recipient", self.names(self.evaluate(self.good_body(), real=True), "block"))

    def test_spammy_generic_email_flagged(self):
        body = ("Dear Sir/Madam,\n\nWe are organizing an AMAZING event!!! ACT NOW, LIMITED TIME GUARANTEED exposure to 10,000 students. "
                "Click here https://a.example https://b.example now. " + "Great opportunity for your brand. " * 12)
        q = self.evaluate(body, subject="URGENT!!! SPONSOR OUR FEST NOW")
        self.assertEqual(q["status"], "needs_review")
        w = self.names(q, "warn")
        self.assertIn("spam_risk", w)
        self.assertIn("accuracy", w)  # 10,000 is not in the brief or facts
        self.assertIn("personalization_in_body", w)

    def test_length_checks(self):
        self.assertIn("length", self.names(self.evaluate("Hi team,\n\nShort."), "block"))
        long = "Hi NimbusForge Cloud team,\n\n" + "word " * 200
        self.assertIn("length", self.names(self.evaluate(long), "warn"))

    def test_greeting_must_match_verified_contact(self):
        body = self.good_body().replace("Hi NimbusForge Cloud team,", "Hi Priya,")
        self.assertIn("greeting", self.names(self.evaluate(body), "warn"))
        named = dict(CONTACT, name="Aarav Mehta")
        body2 = self.good_body().replace("Hi NimbusForge Cloud team,", "Hi Aarav,")
        self.assertNotIn("greeting", self.names(self.evaluate(body2, contact=named), "warn"))

    def test_low_fit_warns(self):
        self.assertIn("relevance", self.names(self.evaluate(self.good_body(), fit=20), "warn"))

    def test_scripted_llm_with_bad_output_ends_needs_review(self):
        bad = {"subject": "Sponsor us!!!", "body": "Dear Sir/Madam,\n\nHope this email finds you well. We are thrilled to announce our event.\n",
               "personalization": [{"sentence": "Hope this email finds you well.", "fact_ids": ["F1"], "why": "x"}], "cta": "", "suggested_ask": "swag"}
        d = generate_draft(ScriptedLLM({"email": bad}), EVENT, CO, CONTACT, facts(), SENDER)
        q = quality.evaluate(d, event_context(EVENT), CO, CONTACT, select_facts(facts()), 80)
        self.assertNotEqual(q["status"], "ok")

    def with_extra(self, extra, ev, co=CO):
        ch = select_facts(facts())
        body = core_body(self.good_body()) + "\n\n" + extra   # quality checks ignore text after the signature
        d = {"subject": "Partnership", "body": body, "personalization": [{"sentence": ch[0]["text"], "fact_ids": [ch[0]["id"]], "why": ""}]}
        return quality.evaluate(d, event_context(ev), co, CONTACT, ch, 80)

    def test_numbers_and_names_from_the_brief_are_not_flagged(self):
        ev = dict(EVENT, college="XYZ University, organised by Club-128")
        self.assertNotIn("accuracy", self.names(self.with_extra("We are Club-128 from XYZ University.", ev), "warn"))
        self.assertIn("accuracy", self.names(self.with_extra("We are Club-128 and expect 4242 people.", ev), "warn"))

    def test_acronyms_from_names_and_facts_are_not_shouting(self):
        ev = dict(EVENT, name="JAIX 2026 Hackathon", college="JIIT Noida")
        co = {"name": "NVIDIA", "industry": "AI"}
        spam = lambda q: next(c for c in q["checks"] if c["name"] == "spam_risk")["status"]  # noqa: E731
        self.assertEqual(spam(self.with_extra("JAIX teams at JIIT will use NVIDIA GPUS and LLMS.", ev, co)), "pass")
        self.assertEqual(spam(self.with_extra("PLEASE REPLY TODAY QUICKLY.", ev, co)), "warn")


if __name__ == "__main__":
    unittest.main()
