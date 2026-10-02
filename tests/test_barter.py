"""Barter (in-kind) sponsorship mode and the event-preset loader (events/example-event.json)."""
import json
import os
import unittest

from app import prompts, quality
from app.cli import load_event_file
from app.emailgen import event_context, is_in_kind, suggest_ask
from tests.helpers import AppCase, EVENT

BARTER = dict(EVENT, requirements=["API credits", "swag and merchandise", "prizes for winners", "software licences"], name="Online Challenge")
PRESET = os.path.join(os.path.dirname(__file__), "..", "events", "example-event.json")


class InKindTests(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_in_kind(BARTER))
        self.assertFalse(is_in_kind(EVENT))                       # default demo event asks for cash sponsorship
        self.assertFalse(is_in_kind({"requirements": ["cash sponsorship", "swag"]}))
        self.assertFalse(is_in_kind({"requirements": []}))

    def test_prompt_forbids_asking_for_money_only_in_barter_mode(self):
        co, ct = {"name": "Acme"}, {"name": None, "role": None}
        facts = [{"id": "F1", "text": "Acme runs a student program.", "source_url": "https://acme.example"}]
        barter = prompts.email_prompt(event_context(BARTER), co, ct, facts, {"name": "S"}, BARTER["requirements"])
        cash = prompts.email_prompt(event_context(EVENT), co, ct, facts, {"name": "S"}, EVENT["requirements"])
        self.assertIn("IN-KIND (barter)", barter)
        self.assertIn("Do NOT ask for money", barter)
        self.assertNotIn("IN-KIND", cash)

    def test_ask_prefers_in_kind_items(self):
        credits = [{"id": "F1", "categories": ["credits"], "text": "x", "source_url": "u"}]
        self.assertEqual(suggest_ask(BARTER, credits), "API credits")
        student = [{"id": "F1", "categories": ["student_program"], "text": "x", "source_url": "u"}]
        self.assertEqual(suggest_ask(BARTER, student), "swag and merchandise")
        hack = [{"id": "F1", "categories": ["hackathon"], "text": "x", "source_url": "u"}]
        self.assertEqual(suggest_ask(BARTER, hack), "prizes for winners")

    def check_for(self, body, event):
        d = {"subject": "Partnership", "body": body, "personalization": []}
        q = quality.evaluate(d, event_context(event), {"name": "Acme"}, {"email": "a@acme.example", "confidence": 0.9}, [], 60)
        return [c for c in q["checks"] if c["name"] == "in_kind_ask"]

    def test_money_in_a_barter_email_is_flagged(self):
        self.assertTrue(self.check_for("Hi Acme team,\n\nCould you provide cash support or a small fund for prizes?", BARTER))
        self.assertTrue(self.check_for("Hi Acme team,\n\nOur budget is tight, so ₹ help would be great.", BARTER))

    def test_in_kind_email_not_flagged_and_cash_events_unaffected(self):
        self.assertFalse(self.check_for("Hi Acme team,\n\nCould you give API credits or licences as prizes?", BARTER))
        self.assertFalse(self.check_for("Hi Acme team,\n\nCould you provide cash sponsorship?", EVENT))


class PresetTests(AppCase):
    def write(self, **over):
        raw = {k: v for k, v in json.load(open(PRESET)).items()}
        raw.update(city="Online", event_type="online coding challenge")
        raw.update(over)
        path = os.path.join(self.tmp, "event.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def test_shipped_preset_refuses_to_run_until_placeholders_are_filled(self):
        with self.assertRaises(SystemExit) as cm:
            load_event_file(PRESET)
        self.assertIn("event_name", str(cm.exception))
        self.assertIn("event_date", str(cm.exception))

    def test_filled_preset_is_valid_and_barter(self):
        path = self.write(event_name="Code Sprint 2026", college="ABC Coding Club", event_date="2026-12-05",
                          expected_attendance="2000", description="A test event.")
        data = load_event_file(path)
        self.assertEqual(data["name"], "Code Sprint 2026")
        self.assertEqual(data["city"], "Online")
        self.assertEqual(data["expected_attendance"], 2000)
        self.assertTrue(is_in_kind(data))
        self.assertNotIn("cash sponsorship", data["requirements"])

    def test_numeric_json_values_are_accepted(self):
        path = self.write(event_name="X", college="Y", event_date="2026-12-05", expected_attendance=250, description="d")
        self.assertEqual(load_event_file(path)["expected_attendance"], 250)

    def test_invalid_values_are_reported(self):
        path = self.write(event_name="X", college="Y", event_date="05-12-2026", expected_attendance="2000", description="d")
        with self.assertRaises(SystemExit) as cm:
            load_event_file(path)
        self.assertIn("event_date", str(cm.exception))

    def test_demo_pipeline_runs_for_a_barter_event(self):
        eid = self.repo.create_event(load_event_file(self.write(event_name="Code Sprint 2026", college="ABC Coding Club",
                                     event_date="2026-12-05", expected_attendance="2000", description="d")), 20, "test")
        self.pipe.discover(eid)
        self.svc.queue.run_all()
        leads = [l for l in self.repo.list_leads(eid) if l["contact_status"] == "found" and l["research_status"] == "COMPLETED"]
        self.assertTrue(leads)
        d = self.repo.get_draft(self.pipe.generate_email(leads[0]["id"]))
        self.assertFalse([c for c in d["quality"]["checks"] if c["name"] == "in_kind_ask"])


if __name__ == "__main__":
    unittest.main()
