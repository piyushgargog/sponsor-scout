import os
import tempfile
import unittest
from unittest import mock

import requests

from app.search.base import SearchError
from app.search.list import ListSearchProvider
from app.search.mock import MockSearchProvider
from app.search.web import WebSearchProvider


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.text = code, data or {}, text

    def json(self):
        return self._d


class WebSearchTests(unittest.TestCase):
    def test_requires_credentials_and_known_backend(self):
        with self.assertRaises(SearchError):
            WebSearchProvider("brave")
        with self.assertRaises(SearchError):
            WebSearchProvider("google_cse", google_key="k")
        with self.assertRaises(SearchError):
            WebSearchProvider("duckduckgo")

    def test_brave_parsing_headers_and_country(self):
        s = mock.MagicMock()
        s.get.return_value = Resp(200, {"web": {"results": [{"title": "A", "url": "https://a.example/", "description": "d"},
                                                             {"title": "no url", "url": "", "description": "x"}]}})
        out = WebSearchProvider("brave", brave_key="BKEY", session=s).search("cloud sponsors", limit=5, country="India")
        self.assertEqual([(r.title, r.url, r.snippet) for r in out], [("A", "https://a.example/", "d")])
        kw = s.get.call_args[1]
        self.assertEqual(kw["headers"]["X-Subscription-Token"], "BKEY")
        self.assertEqual(kw["params"]["country"], "IN")
        self.assertEqual(kw["params"]["q"], "cloud sponsors")

    def test_google_cse_parsing(self):
        s = mock.MagicMock()
        s.get.return_value = Resp(200, {"items": [{"title": "B", "link": "https://b.example/", "snippet": "sn"}]})
        out = WebSearchProvider("google_cse", google_key="K", google_cx="CX", session=s).search("q", limit=50)
        self.assertEqual(out[0].url, "https://b.example/")
        self.assertEqual(s.get.call_args[1]["params"]["num"], 10)  # CSE max page size

    def test_empty_results_and_errors(self):
        s = mock.MagicMock()
        p = WebSearchProvider("brave", brave_key="K", session=s)
        s.get.return_value = Resp(200, {})
        self.assertEqual(p.search("q"), [])
        s.get.return_value = Resp(429, text="rate limited AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123456")
        with self.assertRaises(SearchError) as cm:
            p.search("q")
        self.assertNotIn("AIzaSy", str(cm.exception))
        s.get.side_effect = requests.ConnectionError("down")
        with self.assertRaises(SearchError):
            p.search("q")


class MockSearchTests(unittest.TestCase):
    def test_returns_fictional_demo_companies_only(self):
        out = MockSearchProvider().search("cloud platform company sponsor student hackathon India")
        self.assertTrue(out)
        self.assertTrue(all(r.url.split("/")[2].endswith(".example") for r in out))


if __name__ == "__main__":
    unittest.main()


class ListSearchTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_returns_every_row_with_a_url(self):
        p = self.write("name,url,note\nA,https://a.example/,credits\nNo URL,,x\nB, https://b.example/ ,\n")
        out = ListSearchProvider(p).search("anything", limit=1)
        self.assertEqual([(r.title, r.url, r.snippet) for r in out], [("A", "https://a.example/", "credits"), ("B", "https://b.example/", "")])

    def test_missing_or_empty_file_is_a_clear_error(self):
        with self.assertRaises(SearchError):
            ListSearchProvider("")
        with self.assertRaises(SearchError):
            ListSearchProvider(os.path.join(tempfile.gettempdir(), "does-not-exist.csv"))
        with self.assertRaises(SearchError):
            ListSearchProvider(self.write("name,url,note\n"))
