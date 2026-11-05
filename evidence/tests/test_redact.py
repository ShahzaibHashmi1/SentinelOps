import unittest

import _bootstrap  # noqa: F401

from app import redact as rd


class RedactionTests(unittest.TestCase):
    def assertMasked(self, text, secret):
        out = rd.redact(text)
        self.assertNotIn(secret, out, out)
        self.assertIn(rd.MASK, out)

    def test_postgres_and_redis_urls(self):
        self.assertMasked("connect postgres://app:S3cr3t@postgres:5432/shop failed", "S3cr3t")
        self.assertMasked("redis://:hunter2@redis:6379/0 refused", "hunter2")
        self.assertMasked("postgresql://u:p@ss@host/db", "p@ss")
        self.assertMasked("REDIS://user:pw1@redis", "pw1")

    def test_url_keeps_host_and_path(self):
        out = rd.redact("connect postgres://app:S3cr3t@postgres:5432/shop failed")
        self.assertIn("postgres:5432/shop", out)
        self.assertIn("postgres://", out)

    def test_url_without_credentials_is_untouched(self):
        for text in ("GET http://orders:8000/api/orders 200", "http://host:8080/a@b", "redis://redis:6379/0"):
            self.assertEqual(rd.redact(text), text)

    def test_password_forms(self):
        for text, secret in (
            ("password=abc123 next", "abc123"),
            ("PASSWORD: abc123", "abc123"),
            ("POSTGRES_PASSWORD=abc123", "abc123"),
            ("db_passwd=abc123;", "abc123"),
            ("pwd=abc123", "abc123"),
            ('{"password": "abc 123"}', "abc 123"),
            ("{'password': 'abc123'}", "abc123"),
            ("password = abc123", "abc123"),
            ('password="abc123"', "abc123"),
        ):
            with self.subTest(text):
                self.assertMasked(text, secret)

    def test_token_secret_and_api_key(self):
        for text, secret in (("token=tok123456", "tok123456"), ("api_key=k-999", "k-999"), ("api-key: k-999", "k-999"),
                             ("client_secret=s3", "s3"), ("access_key=AKIA123", "AKIA123"), ('"auth_token": "t0k"', "t0k")):
            with self.subTest(text):
                self.assertMasked(text, secret)

    def test_authorization_header(self):
        for text, secret in (("Authorization: Bearer abcdef123456", "abcdef123456"), ("authorization=Basic dXNlcjpwdw==", "dXNlcjpwdw=="),
                             ("headers {'Authorization': 'Bearer zzzzzz9999'}", "zzzzzz9999"), ("sent Bearer abcdef123456 to api", "abcdef123456")):
            with self.subTest(text):
                self.assertMasked(text, secret)

    def test_idempotent(self):
        text = "postgres://a:b@h/d password=x token=y Authorization: Bearer abcdef123"
        once = rd.redact(text)
        self.assertEqual(rd.redact(once), once)

    def test_ordinary_lines_are_untouched(self):
        for text in ('{"level":"ERROR","event":"db_timeout","request_id":"abc-1"}', "tokenizer finished", "reserve failed for item 12",
                     "connection to server at postgres (10.0.0.2), port 5432 failed"):
            self.assertEqual(rd.redact(text), text)

    def test_json_log_line_with_secret_inside(self):
        line = '{"level":"ERROR","msg":"auth failed for postgres://orders:pw9@postgres/shop","password":"pw9"}'
        out = rd.redact(line)
        self.assertNotIn("pw9", out)
        self.assertIn('"level":"ERROR"', out)


class CleanLineTests(unittest.TestCase):
    def test_truncated_to_300_after_redaction(self):
        line = "password=topsecret " + "x" * 500
        out = rd.clean_line(line)
        self.assertEqual(len(out), 300)
        self.assertNotIn("topsecret", out)

    def test_secret_at_the_cut_is_not_half_kept(self):
        line = "x" * 290 + " password=topsecretvalue"
        out = rd.clean_line(line)
        self.assertNotIn("topsecret", out)
        self.assertLessEqual(len(out), 300)

    def test_short_line_unchanged(self):
        self.assertEqual(rd.clean_line("hello"), "hello")

    def test_custom_limit(self):
        self.assertEqual(rd.clean_line("abcdef", 3), "abc")

    def test_marker_lines_are_withheld(self):
        for line in ("application_name=chaos-F11 lock", "CHAOS-F01-proc started", "wrong password marker chaos-wrong-password-F08"):
            self.assertIsNone(rd.clean_line(line))

    def test_clean_text(self):
        self.assertEqual(rd.clean_text("password=abc"), f"password={rd.MASK}")
        self.assertEqual(rd.clean_text("chaos-F11"), "[withheld]")
        self.assertEqual(len(rd.clean_text("y" * 999)), 300)


class ScrubTests(unittest.TestCase):
    def test_scrub_walks_nested_structures(self):
        data = {"a": ["password=abc", {"b": "fine", "chaos-key": 1}], "c": ("token=tok99999",), "n": 5, "z": None}
        out, withheld = rd.scrub(data)
        self.assertEqual(withheld, 1)
        self.assertEqual(out["a"][0], f"password={rd.MASK}")
        self.assertIn("[withheld]", out["a"][1])
        self.assertEqual(out["c"], [f"token={rd.MASK}"])
        self.assertEqual((out["n"], out["z"]), (5, None))

    def test_scrub_replaces_marker_strings_and_counts(self):
        out, withheld = rd.scrub(["ok", "has chaos in it", "CHAOS again"])
        self.assertEqual(out, ["ok", "[withheld]", "[withheld]"])
        self.assertEqual(withheld, 2)

    def test_scrub_does_not_change_the_input(self):
        data = {"a": ["password=abc"]}
        rd.scrub(data)
        self.assertEqual(data, {"a": ["password=abc"]})


if __name__ == "__main__":
    unittest.main()
