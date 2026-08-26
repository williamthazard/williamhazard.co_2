import hashlib
import json
import os
import shutil
import tempfile
from unittest import mock
from django.core.files.uploadedfile import SimpleUploadedFile
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


from django.utils import timezone
from website.models import LogEntry, LogAsset


@override_settings(DEBUG=False)
class EntriesTests(TestCase):
    def setUp(self):
        self.client = Client()
        patcher = mock.patch.dict("os.environ", {"WRITER_TOKEN_HASH": HASH})
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("website.signals.post_to_bluesky", "website.signals.post_to_mastodon"):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        LogEntry.objects.create(
            title="existing", slug="200101-existing",
            content_markdown="synthetic body", publish_date=timezone.now(),
        )

    def put(self, slug, body):
        return self.client.put(
            f"/api/writer/entries/{slug}",
            data=json.dumps(body), content_type="application/json", **auth(),
        )

    def test_list_newest_first(self):
        r = self.client.get("/api/writer/entries", **auth())
        self.assertEqual(r.status_code, 200)
        entries = json.loads(r.content)["entries"]
        self.assertEqual(entries[0]["slug"], "200101-existing")
        self.assertEqual(set(entries[0]), {"slug", "title", "publish_date"})

    def test_get_full_entry(self):
        r = self.client.get("/api/writer/entries/200101-existing", **auth())
        data = json.loads(r.content)
        self.assertEqual(data["content_markdown"], "synthetic body")
        self.assertFalse(data["share_to_bluesky"])

    def test_get_unknown_is_404(self):
        r = self.client.get("/api/writer/entries/nope", **auth())
        self.assertEqual(r.status_code, 404)

    def test_put_creates(self):
        r = self.put("210101-new", {"title": "new", "content_markdown": "synthetic"})
        self.assertEqual(json.loads(r.content)["status"], "created")
        self.assertTrue(LogEntry.objects.filter(slug="210101-new").exists())

    def test_put_updates_and_reports_updated(self):
        r = self.put("200101-existing", {"title": "existing", "content_markdown": "revised synthetic"})
        self.assertEqual(json.loads(r.content)["status"], "updated")
        self.assertEqual(LogEntry.objects.get(slug="200101-existing").content_markdown, "revised synthetic")

    def test_share_flags_or_on_only(self):
        self.put("200101-existing", {"title": "existing", "content_markdown": "b", "share_to_bluesky": True})
        self.assertTrue(LogEntry.objects.get(slug="200101-existing").share_to_bluesky)
        self.put("200101-existing", {"title": "existing", "content_markdown": "b", "share_to_bluesky": False})
        self.assertTrue(LogEntry.objects.get(slug="200101-existing").share_to_bluesky)  # cannot flip off

    def test_posted_flags_unreachable(self):
        self.put("200101-existing", {"title": "existing", "content_markdown": "b", "posted_to_bluesky": True})
        self.assertFalse(LogEntry.objects.get(slug="200101-existing").posted_to_bluesky)

    def test_missing_title_is_400(self):
        r = self.put("210102-bad", {"content_markdown": "b"})
        self.assertEqual(r.status_code, 400)


ASSETS_MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(
    DEBUG=False,
    MEDIA_ROOT=ASSETS_MEDIA_ROOT,
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    },
)
class AssetsTests(TestCase):
    def setUp(self):
        self.client = Client()
        patcher = mock.patch.dict("os.environ", {"WRITER_TOKEN_HASH": HASH})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entry = LogEntry.objects.create(
            title="existing", slug="200101-existing",
            content_markdown="synthetic body", publish_date=timezone.now(),
        )

    def tearDown(self):
        if os.path.exists(ASSETS_MEDIA_ROOT):
            shutil.rmtree(ASSETS_MEDIA_ROOT)

    def post_asset(self, slug, name, content=b"synthetic-bytes", filename="pig.jpg"):
        uploaded = SimpleUploadedFile(filename, content, content_type="image/jpeg")
        data = {"name": name, "file": uploaded}
        if slug is not None:
            data["slug"] = slug
        return self.client.post("/api/writer/assets", data=data, **auth())

    @mock.patch('threading.Thread')
    def test_upload_new_returns_201_and_stores_file(self, mock_thread):
        r = self.post_asset("200101-existing", "pig.jpg")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(json.loads(r.content)["status"], "uploaded")
        asset = LogAsset.objects.get(log_entry=self.entry)
        self.assertEqual(os.path.basename(asset.file.name), "pig.jpg")
        self.assertTrue(os.path.exists(asset.file.path))

    @mock.patch('threading.Thread')
    def test_reupload_identical_content_is_unchanged_with_no_duplicate(self, mock_thread):
        self.post_asset("200101-existing", "pig.jpg", content=b"synthetic-bytes")
        r = self.post_asset("200101-existing", "pig.jpg", content=b"synthetic-bytes")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["status"], "unchanged")
        self.assertEqual(LogAsset.objects.filter(log_entry=self.entry).count(), 1)

    @mock.patch('threading.Thread')
    def test_reupload_different_content_is_409_and_leaves_original_intact(self, mock_thread):
        self.post_asset("200101-existing", "pig.jpg", content=b"synthetic-bytes")
        asset = LogAsset.objects.get(log_entry=self.entry)
        with asset.file.open("rb") as f:
            original_bytes = f.read()

        r = self.post_asset("200101-existing", "pig.jpg", content=b"different-synthetic-bytes")

        self.assertEqual(r.status_code, 409)
        self.assertIn("error", json.loads(r.content))
        self.assertEqual(LogAsset.objects.filter(log_entry=self.entry).count(), 1)
        asset.refresh_from_db()
        with asset.file.open("rb") as f:
            self.assertEqual(f.read(), original_bytes)

    @mock.patch('threading.Thread')
    def test_unknown_slug_is_404(self, mock_thread):
        r = self.post_asset("nope-does-not-exist", "pig.jpg")
        self.assertEqual(r.status_code, 404)

    @mock.patch('threading.Thread')
    def test_missing_name_is_400(self, mock_thread):
        uploaded = SimpleUploadedFile("pig.jpg", b"synthetic-bytes", content_type="image/jpeg")
        r = self.client.post(
            "/api/writer/assets",
            data={"slug": "200101-existing", "file": uploaded},
            **auth(),
        )
        self.assertEqual(r.status_code, 400)
