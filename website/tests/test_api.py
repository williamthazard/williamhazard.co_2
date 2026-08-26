import hashlib
import json
from unittest import mock
from django.test import TestCase, Client, override_settings

TOKEN = "synthetic-test-token-not-real"
HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


def auth(token=TOKEN):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@override_settings(DEBUG=False)
class PingAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        patcher = mock.patch.dict("os.environ", {"WRITER_TOKEN_HASH": HASH})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ping_with_valid_token(self):
        r = self.client.get("/api/writer/ping", **auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True})

    def test_ping_missing_token_is_401(self):
        r = self.client.get("/api/writer/ping")
        self.assertEqual(r.status_code, 401)

    def test_ping_wrong_token_is_401(self):
        r = self.client.get("/api/writer/ping", **auth("wrong-token"))
        self.assertEqual(r.status_code, 401)


class PingDebugTests(TestCase):
    def test_debug_without_hash_allows(self):
        with mock.patch.dict("os.environ", clear=False):
            import os
            os.environ.pop("WRITER_TOKEN_HASH", None)
            with self.settings(DEBUG=True):
                r = Client().get("/api/writer/ping")
        self.assertEqual(r.status_code, 200)


class MintCommandTests(TestCase):
    def test_mint_prints_token_and_matching_hash(self):
        import io
        from django.core.management import call_command
        out = io.StringIO()
        call_command("mint_writer_token", stdout=out)
        text = out.getvalue()
        lines = [l for l in text.splitlines() if l.strip()]
        token_line = next(l for l in lines if l.startswith("BLOG_WRITER_TOKEN="))
        hash_line = next(l for l in lines if l.startswith("WRITER_TOKEN_HASH="))
        token = token_line.split("=", 1)[1]
        digest = hash_line.split("=", 1)[1]
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), digest)
        self.assertGreaterEqual(len(token), 32)
