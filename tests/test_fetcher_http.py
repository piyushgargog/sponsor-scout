"""HttpFetcher against a real local HTTP server (real sockets, redirects, robots.txt). The SSRF guard is patched
off only for the scenarios that need loopback; the final test proves it is on by default."""
import http.server
import socketserver
import threading
import unittest
from unittest import mock

from app.fixtures.mock_data import COMPANIES
from app.research.contacts import discover_contacts
from app.research.extract import extract_facts, extract_people
from app.research.fetcher import HttpFetcher, crawl_company

NIM = next(c for c in COMPANIES if c["name"].startswith("NimbusForge"))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body, ct = b"User-agent: *\nDisallow: /careers", "text/plain"
        elif path == "/old-about":
            self.send_response(301)
            self.send_header("Location", "/about")
            self.end_headers()
            return
        elif path == "/to-private":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        elif path == "/big":
            body, ct = b"<p>" + b"x" * 3_000_000 + b"</p>", "text/html"
        elif path == "/pdf":
            body, ct = b"%PDF-1.4", "application/pdf"
        else:
            html = NIM["pages"].get(path.rstrip("/") or "/")
            if not html:
                self.send_response(404)
                self.end_headers()
                return
            body, ct = html.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class HttpFetcherIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        cls.srv.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        p = mock.patch("app.research.fetcher.is_public_host", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.f = HttpFetcher(delay=0, timeout=5)

    def test_fetch_follows_redirect_and_parses(self):
        self.assertEqual(self.f.fetch(self.base + "/").title, "NimbusForge Cloud")
        self.assertEqual(self.f.fetch(self.base + "/old-about").title, "About NimbusForge")

    def test_robots_txt_respected(self):
        self.assertIsNone(self.f.fetch(self.base + "/careers"))
        self.assertIsNotNone(self.f.fetch(self.base + "/about"))

    def test_rejects_non_html_404_and_truncates_oversize(self):
        self.assertIsNone(self.f.fetch(self.base + "/pdf"))
        self.assertIsNone(self.f.fetch(self.base + "/nope"))
        self.assertLessEqual(len(self.f.fetch(self.base + "/big").text), 400_000)

    def test_crawl_extracts_facts_and_contacts_over_http(self):
        pages = crawl_company(self.f, self.base + "/", 8)
        urls = {p.url.replace(self.base, "") for p in pages}
        self.assertTrue({"/", "/about", "/community", "/contact", "/partners", "/students"} <= urls)
        self.assertNotIn("/careers", urls)
        self.assertGreaterEqual(len(extract_facts(pages)), 5)
        emails = [c["email"] for c in discover_contacts(pages, "nimbusforge.example", extract_people(pages))]
        self.assertIn("partnerships@nimbusforge.example", emails)

    def test_redirect_to_metadata_address_is_refused(self):
        real = lambda h: h == "127.0.0.1"  # noqa: E731  (loopback allowed for the test server only)
        with mock.patch("app.research.fetcher.is_public_host", side_effect=real):
            self.assertIsNone(HttpFetcher(delay=0, timeout=5)._get(self.base + "/to-private"))


class SsrfDefaultTests(unittest.TestCase):
    def test_loopback_blocked_without_patching(self):
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertIsNone(HttpFetcher(delay=0, timeout=5).fetch(f"http://127.0.0.1:{srv.server_address[1]}/"))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
