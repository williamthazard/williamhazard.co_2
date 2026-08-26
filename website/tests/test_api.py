import hashlib
import json
import os
import shutil
import tempfile
import uuid
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


class DraftPreviewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.drafts_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.drafts_dir, ignore_errors=True)
        patcher = mock.patch.dict("os.environ", {"LOG_DRAFTS_DIR": self.drafts_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_draft(self, slug, text):
        path = os.path.join(self.drafts_dir, f"{slug}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    @override_settings(DEBUG=True)
    def test_renders_title_and_body_through_real_template(self):
        self.write_draft(
            "240101-draft",
            "title: Draft Title\nslug: 240101-draft\n\nsynthetic body for testing.\n",
        )
        r = self.client.get("/draft-preview/240101-draft/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Draft Title", body)
        self.assertIn("synthetic body for testing", body)

    @override_settings(DEBUG=True)
    def test_mtime_returns_float_and_changes_on_touch(self):
        path = self.write_draft(
            "240101-mtime", "title: T\nslug: 240101-mtime\n\nsynthetic body.\n"
        )
        r = self.client.get("/draft-preview/240101-mtime/mtime")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content)
        self.assertIsInstance(data["mtime"], float)
        first = data["mtime"]
        os.utime(path, (first + 100, first + 100))
        r2 = self.client.get("/draft-preview/240101-mtime/mtime")
        second = json.loads(r2.content)["mtime"]
        self.assertNotEqual(first, second)

    def test_not_debug_preview_is_404(self):
        self.write_draft("240101-nd", "title: T\nslug: 240101-nd\n\nsynthetic body.\n")
        with self.settings(DEBUG=False):
            r = self.client.get("/draft-preview/240101-nd/")
        self.assertEqual(r.status_code, 404)

    def test_not_debug_mtime_is_404(self):
        self.write_draft("240101-nd2", "title: T\nslug: 240101-nd2\n\nsynthetic body.\n")
        with self.settings(DEBUG=False):
            r = self.client.get("/draft-preview/240101-nd2/mtime")
        self.assertEqual(r.status_code, 404)

    @override_settings(DEBUG=True)
    def test_malformed_header_is_200_naming_line(self):
        self.write_draft(
            "240101-bad",
            "title: T\nbadline-no-colon\nslug: 240101-bad\n\nsynthetic body.\n",
        )
        r = self.client.get("/draft-preview/240101-bad/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("line 2", r.content.decode())

    @override_settings(DEBUG=True)
    def test_missing_required_field_is_200_naming_line(self):
        self.write_draft("240101-noslug", "title: T\n\nsynthetic body.\n")
        r = self.client.get("/draft-preview/240101-noslug/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("slug", r.content.decode())

    @override_settings(DEBUG=True)
    def test_unknown_slug_is_404(self):
        r = self.client.get("/draft-preview/does-not-exist-anywhere/")
        self.assertEqual(r.status_code, 404)

    @override_settings(DEBUG=True)
    def test_reload_script_injected_and_polls_mtime(self):
        self.write_draft(
            "240101-reload", "title: T\nslug: 240101-reload\n\nsynthetic body.\n"
        )
        r = self.client.get("/draft-preview/240101-reload/")
        body = r.content.decode()
        self.assertIn("<script>", body)
        self.assertIn("/draft-preview/240101-reload/mtime", body)
        self.assertIn("1500", body)
        self.assertTrue(body.rstrip().endswith("</body>") or "</body>" in body)


class DraftAssetFallbackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.drafts_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.drafts_dir, ignore_errors=True)
        patcher = mock.patch.dict("os.environ", {"LOG_DRAFTS_DIR": self.drafts_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asset_name = f"fallback-{uuid.uuid4().hex}.png"
        assets_dir = os.path.join(self.drafts_dir, "240101-draft.assets")
        os.makedirs(assets_dir)
        with open(os.path.join(assets_dir, self.asset_name), "wb") as f:
            f.write(b"synthetic-fallback-bytes")

    @override_settings(DEBUG=True)
    def test_serves_draft_asset_fallback_in_debug(self):
        r = self.client.get(f"/media/log_assets/{self.asset_name}")
        self.assertEqual(r.status_code, 200)
        content = b"".join(r.streaming_content)
        self.assertEqual(content, b"synthetic-fallback-bytes")

    def test_fallback_inert_outside_debug(self):
        with self.settings(DEBUG=False):
            r = self.client.get(f"/media/log_assets/{self.asset_name}")
        self.assertEqual(r.status_code, 404)

    @override_settings(DEBUG=True)
    def test_unknown_asset_still_404_in_debug(self):
        r = self.client.get("/media/log_assets/totally-nonexistent-name.png")
        self.assertEqual(r.status_code, 404)
