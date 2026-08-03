import os
import shutil
import tempfile
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from website.models import Page, PageAsset, LogEntry, LogAsset

TEMP_MEDIA_ROOT = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class ModelsTestCase(TestCase):
    def tearDown(self):
        if os.path.exists(TEMP_MEDIA_ROOT):
            shutil.rmtree(TEMP_MEDIA_ROOT)

    def test_page_creation(self):
        page = Page.objects.create(title="Test Page", slug="test-page", content_markdown="Some content")
        self.assertEqual(str(page), "Test Page")
        self.assertEqual(page.slug, "test-page")

    def test_log_entry_creation(self):
        now = timezone.now()
        entry = LogEntry.objects.create(title="Test Log", slug="240809-test", content_markdown="Log content", publish_date=now)
        self.assertEqual(str(entry), "Test Log")
        self.assertEqual(entry.slug, "240809-test")

    @patch('threading.Thread')
    def test_page_asset_saving(self, mock_thread):
        page = Page.objects.create(title="Test", slug="test", content_markdown="content")
        uploaded = SimpleUploadedFile("test.jpg", b"fakeimagebytes", content_type="image/jpeg")
        asset = PageAsset.objects.create(page=page, file=uploaded)
        self.assertTrue(asset.file.name.startswith("page_assets/"))

    @patch('threading.Thread')
    def test_log_asset_saving(self, mock_thread):
        entry = LogEntry.objects.create(title="Log", slug="log-slug", content_markdown="content", publish_date=timezone.now())
        uploaded = SimpleUploadedFile("log.jpg", b"fakeimagebytes", content_type="image/jpeg")
        asset = LogAsset.objects.create(log_entry=entry, file=uploaded)
        self.assertTrue(asset.file.name.startswith("log_assets/"))
