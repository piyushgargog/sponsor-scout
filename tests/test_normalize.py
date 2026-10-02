import unittest

from app.normalize import (is_placeholder_email, is_valid_email, normalize_company_name, normalize_contact_name, normalize_domain,
                           normalize_email)


class NormalizeTests(unittest.TestCase):
    def test_company_name_variants_collapse(self):
        self.assertEqual(normalize_company_name("Acme Technologies Pvt. Ltd."), "acme technologies")
        self.assertEqual(normalize_company_name("ACME  Technologies, Inc."), "acme technologies")
        self.assertEqual(normalize_company_name("AT&T Inc"), "at and t")
        self.assertEqual(normalize_company_name("Café Étoile LLC"), "cafe etoile")
        self.assertEqual(normalize_company_name(""), "")
        self.assertEqual(normalize_company_name("Limited"), "limited")  # never reduce a name to nothing

    def test_domain(self):
        self.assertEqual(normalize_domain("https://www.Example.com/path?x=1"), "example.com")
        self.assertEqual(normalize_domain("docs.nimbusforge.example"), "nimbusforge.example")
        self.assertEqual(normalize_domain("https://shop.foo.co.in"), "foo.co.in")
        self.assertEqual(normalize_domain("https://myapp.vercel.app/x"), "myapp.vercel.app")
        for bad in ("", None, "http://127.0.0.1/", "localhost", "not a url"):
            self.assertIsNone(normalize_domain(bad), bad)

    def test_email_validation(self):
        for ok in ("a@b.co", "first.last+tag@sub.example.com", "partnerships@nimbusforge.example"):
            self.assertTrue(is_valid_email(ok), ok)
        for bad in ("", None, "plain", "a@b", "a@@b.com", "a b@c.com", "a@b.com\nBcc: x@y.com", "a..b@c.com", ".a@b.com", "a@b.c1", "a@b.com,c@d.com", "<a@b.com>"):
            self.assertFalse(is_valid_email(bad), bad)

    def test_email_normalization(self):
        self.assertEqual(normalize_email(" Sales+Event@Example.COM "), "sales@example.com")
        self.assertEqual(normalize_email("mailto:Hello@x.io?subject=hi"), "hello@x.io")
        self.assertEqual(normalize_email("J.o.h.n+x@gmail.com"), "john@gmail.com")
        self.assertEqual(normalize_email("john@googlemail.com"), "john@gmail.com")
        self.assertIsNone(normalize_email("nope"))

    def test_placeholder_domains(self):
        self.assertTrue(is_placeholder_email("a@nimbusforge.example"))
        self.assertTrue(is_placeholder_email("a@example.com"))
        self.assertFalse(is_placeholder_email("a@real-company.io"))

    def test_contact_name(self):
        self.assertEqual(normalize_contact_name("  aarav   MEHTA "), "Aarav Mehta")
        self.assertEqual(normalize_contact_name(None), "")


if __name__ == "__main__":
    unittest.main()
