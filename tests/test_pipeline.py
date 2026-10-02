import sqlite3
import unittest
from unittest import mock

from app.jobs import PermanentError
from app.llm.base import LLMError
from app.research.fetcher import FixtureFetcher
from app.search.base import SearchProvider, SearchResult
from app.util import iso_ago
from tests.helpers import AppCase, EVENT, ScriptedLLM


class CountingFetcher(FixtureFetcher):
    def __init__(self):
        self.n = 0

    def fetch(self, url):
        self.n += 1
        return super().fetch(url)


class DiscoveryTests(AppCase):
    def test_discovery_finds_filters_and_dedupes(self):
        r = self.pipe.discover(self.event_id, research=False)
        names = {l["company_name"] for l in self.repo.list_leads(self.event_id)}
        self.assertEqual(len(names), 9)
        self.assertNotIn("Harbor & Pine Furniture", names)  # irrelevant company filtered out
        self.assertGreaterEqual(r["rejected"], 1)
        r2 = self.pipe.discover(self.event_id, research=False)  # run again: nothing new
        self.assertEqual(r2["leads_created"], 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM leads"), 9)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM companies"), 9)

    def test_companies_not_hardcoded_in_pipeline_code(self):
        import pathlib
        src = "\n".join(p.read_text() for p in pathlib.Path("app").rglob("*.py") if "fixtures" not in str(p) and "search/mock" not in str(p))
        for c in ("NimbusForge", "Tensorloom", "QueryDock", "Microsoft", "Amazon", "Stripe", "Vercel Inc", "Datadog"):
            self.assertNotIn(c, src, c)

    def test_social_and_news_hosts_skipped(self):
        class S(SearchProvider):
            name = "s"
            def search(self, q, limit=10, country=None):
                return [SearchResult("Acme on LinkedIn", "https://www.linkedin.com/company/acme", "dev tools"),
                        SearchResult("Some College", "https://www.somecollege.edu.in/", "students"),
                        SearchResult("Acme | developer cloud", "https://acme-cloud.example/", "Acme builds a cloud API platform for developers")]
        self.svc.pipeline.search = S()
        r = self.pipe.discover(self.event_id, research=False)
        self.assertEqual([l["domain"] for l in self.repo.list_leads(self.event_id)], ["acme-cloud.example"])

    def test_model_inventing_a_domain_is_ignored(self):
        llm = ScriptedLLM({"candidate_classify": {"companies": [{"domain": "totally-made-up.com", "is_company": True, "company_name": "Made Up", "relevance": 0.99}]}})
        self.svc.pipeline.llm = llm
        self.pipe.discover(self.event_id, research=False)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM companies"), 0)

    def test_query_generation_failure_falls_back(self):
        self.svc.pipeline.llm = ScriptedLLM({"discovery_queries": LLMError("boom")})
        r = self.pipe.discover(self.event_id, research=False)
        self.assertGreater(r["queries"], 0)

    def test_duplicate_company_variants_merge(self):
        a, new_a = self.repo.upsert_company("Acme Technologies Pvt Ltd", "https://www.acme.example/", "x")
        b, new_b = self.repo.upsert_company("ACME Technologies", "https://acme.example/about", "x")
        c, new_c = self.repo.upsert_company("Acme Labs", "https://www.acme.example/", "x")  # same domain, different name
        self.assertEqual((a, b, c), (a, a, a))
        self.assertTrue(new_a and not new_b and not new_c)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.run("INSERT INTO companies(name,normalized_name,domain,website,created_at) VALUES('X','zzz','acme.example','w','t')")


class ResearchTests(AppCase):
    def setUp(self):
        self.fetcher = CountingFetcher()
        self.svc_kw = {"fetcher": self.fetcher}
        super().setUp()
        self.pipe.discover(self.event_id, research=False)

    def test_cache_reused_until_refresh_or_expiry(self):
        lead = self.lead_by_name("QueryDock")
        self.pipe.research_lead(lead["id"])
        n = self.fetcher.n
        self.assertGreater(n, 0)
        with mock.patch("app.pipeline.log_event") as lg:
            self.pipe.research_lead(lead["id"])
        self.assertEqual(self.fetcher.n, n)  # cache hit: no new fetches
        self.assertTrue(any(c.args[0] == "research_cache_hit" for c in lg.call_args_list))
        self.pipe.research_lead(lead["id"], force=True)  # Refresh Research
        self.assertGreater(self.fetcher.n, n)
        n2 = self.fetcher.n
        self.db.run("UPDATE company_research SET researched_at=?", (iso_ago(days=60),))
        self.pipe.research_lead(lead["id"])
        self.assertGreater(self.fetcher.n, n2)  # stale -> re-researched

    def test_cache_is_per_research_version(self):
        lead = self.lead_by_name("QueryDock")
        self.pipe.research_lead(lead["id"])
        n = self.fetcher.n
        self.settings.research_version = "v2"
        self.pipe.research_lead(lead["id"])
        self.assertGreater(self.fetcher.n, n)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM company_research"), 2)

    def test_cache_shared_across_events(self):
        lead = self.lead_by_name("QueryDock")
        self.pipe.research_lead(lead["id"])
        n = self.fetcher.n
        ev2 = self.repo.create_event(dict(EVENT, name="Second Fest"), 20)
        lid2, _ = self.repo.ensure_lead(ev2, lead["company_id"])
        self.pipe.research_lead(lid2)
        self.assertEqual(self.fetcher.n, n)
        self.assertIsNotNone(self.repo.get_lead(lid2)["fit_score"])

    def test_unverifiable_llm_claims_are_discarded(self):
        fake = {"summary": "S", "industry": "Database", "products": [
            {"text": "Won an award", "quote": "QueryDock won the 2026 global database innovation award", "source_url": "https://www.querydock.example/"},
            {"text": "ok", "quote": "QueryDock is an open-source database that gives developers fast analytics on their own data.", "source_url": "https://www.querydock.example/"}],
            "launches": [{"text": "x", "quote": "We launched a rocket to the moon yesterday evening", "source_url": "https://www.querydock.example/"}]}
        self.svc.pipeline.llm = ScriptedLLM({"research_synthesis": fake})
        lead = self.lead_by_name("QueryDock")
        self.pipe.research_lead(lead["id"])
        r = self.repo.get_research(lead["company_id"], self.settings.research_version)
        self.assertEqual(len(r["profile"]["products"]), 1)
        self.assertEqual(r["profile"]["launches"], [])
        self.assertEqual(r["stats"]["claims_rejected"], 2)

    def test_llm_failure_during_synthesis_does_not_break_research(self):
        self.svc.pipeline.llm = ScriptedLLM({"research_synthesis": LLMError("down")})
        lead = self.lead_by_name("QueryDock")
        self.pipe.research_lead(lead["id"])
        r = self.repo.get_research(lead["company_id"], self.settings.research_version)
        self.assertEqual(r["status"], "COMPLETED")
        self.assertNotEqual(r["profile"]["summary"], "")

    def test_unreachable_site_marks_failed_not_fabricated(self):
        cid, _ = self.repo.upsert_company("Ghost Co", "https://ghost.example/", "x")
        lid, _ = self.repo.ensure_lead(self.event_id, cid)
        with self.assertRaises(PermanentError):
            self.pipe.research_lead(lid)
        self.assertEqual(self.repo.get_lead(lid)["research_status"], "FAILED")
        self.assertEqual(self.repo.contacts_for(cid), [])

    def test_contact_not_found_blocks_email_generation(self):
        lead = self.lead_by_name("DeployDen Hosting")
        self.pipe.research_lead(lead["id"])
        lead = self.repo.get_lead(lead["id"])
        self.assertEqual(lead["contact_status"], "not_found")
        self.assertIsNone(lead["primary_contact_id"])
        with self.assertRaises(PermanentError):
            self.pipe.generate_email(lead["id"])

    def test_manual_contact_enables_drafting_and_is_validated(self):
        lead = self.lead_by_name("DeployDen Hosting")
        self.pipe.research_lead(lead["id"])
        with self.assertRaises(ValueError):
            self.pipe.add_manual_contact(lead["id"], "garbage", None, None, "t")
        self.pipe.add_manual_contact(lead["id"], "Events@DeployDen.example", "Sam", "Events", "t")
        did = self.pipe.generate_email(lead["id"])
        self.assertTrue(self.repo.get_draft(did))

    def test_stats_funnel(self):
        for l in self.repo.list_leads(self.event_id):
            self.pipe.research_lead(l["id"])
        st = self.repo.stats(self.event_id, 70)
        self.assertEqual((st["companies"], st["researched"]), (9, 9))
        self.assertEqual(st["with_contact"], 8)
        self.assertGreaterEqual(st["high_fit"], 2)


if __name__ == "__main__":
    unittest.main()
