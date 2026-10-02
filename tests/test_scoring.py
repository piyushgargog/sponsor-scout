import unittest

from app.fixtures.mock_data import COMPANIES
from app.research.extract import extract_facts
from app.research.fetcher import FixtureFetcher, crawl_company
from app.scoring import RUBRIC, score_lead
from tests.helpers import EVENT


def research_for(c):
    pages = crawl_company(FixtureFetcher(), f"https://www.{c['domain']}/")
    return {"facts": extract_facts(pages), "profile": {"summary": c["snippet"], "homepage_url": pages[0].url, "products": []}}, pages


def co(c):
    return {"name": c["name"], "industry": c["industry"], "description": c["snippet"], "website": f"https://www.{c['domain']}/"}


class ScoringTests(unittest.TestCase):
    def test_rubric_sums_to_100(self):
        self.assertEqual(sum(m for _, _, m in RUBRIC), 100)

    def test_no_research_means_low_score_and_unknowns(self):
        r = score_lead(EVENT, {"name": "Ghost", "industry": "unknown", "description": "", "website": "https://ghost.example"}, None)
        self.assertLessEqual(r["score"], 5)
        self.assertEqual(r["evidence"], [])
        self.assertTrue(any("unknown" in x for v in r["breakdown"].values() for x in v["reasons"]))

    def test_scores_bounded_and_explained(self):
        for c in COMPANIES:
            res, _ = research_for(c)
            r = score_lead(EVENT, co(c), res)
            self.assertTrue(0 <= r["score"] <= 100)
            self.assertEqual(r["score"], sum(v["score"] for v in r["breakdown"].values()))
            for v in r["breakdown"].values():
                self.assertTrue(0 <= v["score"] <= v["max"])
                self.assertTrue(v["reasons"])

    def test_every_evidence_item_has_a_real_source(self):
        for c in COMPANIES:
            res, pages = research_for(c)
            urls = {p.url for p in pages}
            for e in score_lead(EVENT, co(c), res)["evidence"]:
                self.assertIn(e["source"], urls)
                self.assertTrue(e["claim"])

    def test_ranking_makes_sense(self):
        scores = {}
        for c in COMPANIES:
            res, _ = research_for(c)
            scores[c["name"]] = score_lead(EVENT, co(c), res)["score"]
        self.assertGreaterEqual(scores["NimbusForge Cloud"], 70)
        self.assertGreaterEqual(scores["Tensorloom AI"], 70)
        self.assertLess(scores["Harbor & Pine Furniture"], 20)
        self.assertGreater(scores["NimbusForge Cloud"], scores["ShieldKernel Security"])

    def test_india_presence_not_guessed(self):
        c = next(x for x in COMPANIES if x["name"] == "ShieldKernel Security")
        res, _ = research_for(c)
        r = score_lead(EVENT, co(c), res)
        self.assertEqual(r["breakdown"]["india_presence"]["score"], 0)

    def test_deterministic(self):
        c = COMPANIES[0]
        res, _ = research_for(c)
        self.assertEqual(score_lead(EVENT, co(c), res), score_lead(EVENT, co(c), res))


if __name__ == "__main__":
    unittest.main()
