import unittest
from unittest import mock

from app.fixtures.mock_data import COMPANIES
from app.research.contacts import contact_is_usable, discover_contacts
from app.research.extract import extract_facts, extract_people, verify_quote
import time

from app.research.fetcher import FixtureFetcher, HttpFetcher, Page, crawl_company, find_emails, is_public_host, parse_html


def pages_for(domain):
    return crawl_company(FixtureFetcher(), f"https://www.{domain}/")


class FactTests(unittest.TestCase):
    def test_facts_are_verbatim_and_sourced(self):
        for c in COMPANIES:
            pages = pages_for(c["domain"])
            by_url = {p.url: p.text for p in pages}
            for f in extract_facts(pages):
                self.assertIn(f["text"], by_url[f["source_url"]].replace("\n", " ") + "\n".join(by_url[f["source_url"]].split("\n")), c["name"])
                self.assertTrue(f["source_url"].startswith("https://"))
                self.assertRegex(f["id"], r"^F\d+$")

    def test_sponsorship_requires_event_context(self):
        pg = Page(url="https://x.example/", text="We sponsor the local football team and our office dog.\nNimbus sponsored four student hackathons in India in 2025.")
        facts = extract_facts([pg])
        cats = [f["categories"] for f in facts]
        self.assertTrue(any("sponsorship" in c for c in cats))
        self.assertFalse(any("football" in f["text"] and "sponsorship" in f["categories"] for f in facts))

    def test_quote_verification(self):
        pages = pages_for("nimbusforge.example")
        home = pages[0]
        self.assertTrue(verify_quote("NimbusForge is a cloud platform that gives developers managed compute", home.url, pages))
        self.assertFalse(verify_quote("NimbusForge won the 2026 global innovation award for cloud", home.url, pages))
        self.assertFalse(verify_quote("NimbusForge is a cloud platform that gives developers managed compute", "https://other.example/", pages))
        self.assertFalse(verify_quote("short", home.url, pages))

    def test_people_extraction(self):
        ppl = extract_people(pages_for("nimbusforge.example"))
        self.assertEqual(ppl[0]["name"], "Aarav Mehta")
        self.assertIn("Developer Relations", ppl[0]["role"])


class ContactTests(unittest.TestCase):
    def test_contacts_only_come_from_pages(self):
        for c in COMPANIES:
            pages = pages_for(c["domain"])
            blob = " ".join(p.text for p in pages).lower() + " ".join(e["email"].lower() for p in pages for e in p.emails)
            for ct in discover_contacts(pages, c["domain"], extract_people(pages)):
                self.assertIn(ct["email"].split("@")[0], blob)
                self.assertTrue(ct["source_url"].startswith("https://"))
                self.assertTrue(0 < ct["confidence"] <= 0.95)

    def test_no_guessing_when_no_email(self):
        pages = pages_for("deployden.example")
        self.assertEqual(discover_contacts(pages, "deployden.example", []), [])

    def test_priority_order_and_exclusions(self):
        html = ('<html><body><p>Press: press@acme.example, no-reply@acme.example, privacy@acme.example, info@acme.example</p>'
                '<p><a href="mailto:sponsorship@acme.example">Sponsorship</a> <a href="mailto:marketing@acme.example">m</a> founder@acme.example</p></body></html>')
        pg = parse_html("https://acme.example/contact", html)
        out = discover_contacts([pg], "acme.example", [])
        self.assertEqual([c["email"] for c in out], ["sponsorship@acme.example", "marketing@acme.example", "founder@acme.example", "info@acme.example"])

    def test_other_brands_and_sample_addresses_are_dropped(self):
        html = ("<p>team@gmail.com, mitch@otheragency.example, hello@company.com, bot@example.com, happy@acme-labs.io</p>"
                "<p>community@acme.dev</p>")
        out = discover_contacts([parse_html("https://acme.example/contact", html)], "acme.example", [])
        self.assertEqual([c["email"] for c in out], ["community@acme.dev"])   # same brand on another TLD is kept
        self.assertLessEqual(out[0]["confidence"], 0.7)

    def test_non_sponsorship_inboxes_are_dropped(self):
        html = ("<p>fraud@acme.example billing-support@acme.example cloudsupport@acme.example help@acme.example "
                "awsreinvent-support@acme.example %20partners@acme.example</p>")
        out = discover_contacts([parse_html("https://acme.example/contact", html)], "acme.example", [])
        self.assertEqual([c["email"] for c in out], ["partners@acme.example"])

    def test_link_text_is_not_a_person_name(self):
        html = ("<p><a href='mailto:help-events@acme.example'>Report Abuse</a> <a href='mailto:partners@acme.example'>Contact Us</a> "
                "<a href='mailto:jane@acme.example'>Jane Doe</a></p>")
        out = {c["email"]: c["name"] for c in discover_contacts([parse_html("https://acme.example/contact", html)], "acme.example", [])}
        self.assertEqual(out, {"help-events@acme.example": None, "partners@acme.example": None, "jane@acme.example": "Jane Doe"})

    def test_contact_is_usable(self):
        self.assertTrue(contact_is_usable("sponsorship@acme.example", "acme.example"))
        self.assertFalse(contact_is_usable("sponsorship@other.example", "acme.example"))
        self.assertFalse(contact_is_usable("billing-support@acme.example", "acme.example"))

    def test_person_attached_when_nearby(self):
        pages = pages_for("tensorloom.example")
        out = {c["email"]: c for c in discover_contacts(pages, "tensorloom.example", extract_people(pages))}
        self.assertEqual(out["riya@tensorloom.example"]["name"], "Riya Sen")


class FetcherSafetyTests(unittest.TestCase):
    def test_private_hosts_rejected(self):
        for h in ("localhost", "127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"):
            self.assertFalse(is_public_host(h), h)

    def test_http_fetcher_never_requests_private_addresses(self):
        sess = mock.MagicMock()
        f = HttpFetcher(delay=0, session=sess)
        self.assertIsNone(f.fetch("http://169.254.169.254/latest/meta-data/"))
        self.assertIsNone(f.fetch("file:///etc/passwd"))
        sess.get.assert_not_called()

    def test_redirect_to_private_host_blocked(self):
        class R:
            status_code, headers = 302, {"Location": "http://127.0.0.1/admin"}
            def close(self): pass
        sess = mock.MagicMock()
        sess.get.return_value = R()
        f = HttpFetcher(delay=0, session=sess)
        with mock.patch("app.research.fetcher.is_public_host", side_effect=lambda h: h != "127.0.0.1"):
            self.assertIsNone(f._get("https://public.example/"))
        self.assertEqual(sess.get.call_count, 1)

    def test_robots_disallow_respected(self):
        class Resp:
            def __init__(self, code, text=""): self.status_code, self.text = code, text
        sess = mock.MagicMock()
        sess.get.return_value = Resp(200, "User-agent: *\nDisallow: /private")
        f = HttpFetcher(delay=0, session=sess)
        with mock.patch("app.research.fetcher.is_public_host", return_value=True):
            self.assertFalse(f.allowed("https://x.example/private/a"))
            self.assertTrue(f.allowed("https://x.example/about"))

    def test_crawl_respects_page_cap(self):
        self.assertEqual(len(crawl_company(FixtureFetcher(), "https://www.nimbusforge.example/", max_pages=3)), 3)

    def test_hostile_pages_cannot_stall_parsing(self):
        """Regression: an unbounded email regex backtracked quadratically on big pages with no '@' and froze a worker."""
        for html in ("<p>" + "x" * 1_400_000 + "</p>", "<p>" + "a@" * 400_000 + "</p>", "<p>" + ("a." * 300_000) + "</p>"):
            t = time.time()
            pg = parse_html("https://a.example/", html)
            self.assertLess(time.time() - t, 3.0)
            self.assertLessEqual(len(pg.text), 400_000)
            self.assertLessEqual(len(pg.emails), 300)

    def test_find_emails(self):
        got = dict(find_emails("Write to Sales.Team+x@Sub.Example.co.in. Or (a_b@c-d.org), not foo@bar or @x.com"))
        self.assertEqual(set(got), {"Sales.Team+x@Sub.Example.co.in", "a_b@c-d.org"})

    def test_parser_ignores_scripts(self):
        pg = parse_html("https://a.example/", "<script>var e='x@y.com'</script><p>Hello world is a long enough sentence here.</p>")
        self.assertEqual(pg.emails, [])
        self.assertNotIn("var e", pg.text)


if __name__ == "__main__":
    unittest.main()
