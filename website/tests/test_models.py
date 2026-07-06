from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch, MagicMock
import os
import tempfile
import shutil
from website.models import Page, LogEntry, LogAsset

# Create a temporary directory for media files during tests
TEMP_MEDIA_ROOT = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class ModelTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

    def test_create_page(self):
        page = Page.objects.create(title="Words", slug="words", content_markdown="Some text")
        self.assertEqual(str(page), "Words")

    def test_create_log_entry(self):
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        self.assertEqual(str(entry), "230919-bear")
        self.assertFalse(entry.posted_to_bluesky)
        self.assertFalse(entry.posted_to_mastodon)

    @patch('threading.Thread')
    def test_create_log_asset_custom_filename(self, mock_thread):
        # We need a LogEntry first
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        # Create a temp file to upload
        file_content = b"fake image content"
        uploaded_file = SimpleUploadedFile("test_image.jpg", file_content, content_type="image/jpeg")
        
        asset = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file,
            custom_filename="custom_bear.jpg"
        )
        # Check custom filename logic
        self.assertTrue(asset.file.name.endswith("custom_bear.jpg"))
        # Check thread was started
        mock_thread.assert_called_once()

    @patch('threading.Thread')
    def test_create_log_asset_auto_filename(self, mock_thread):
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        file_content = b"fake image content"
        uploaded_file_1 = SimpleUploadedFile("test_image.jpg", file_content, content_type="image/jpeg")
        uploaded_file_2 = SimpleUploadedFile("another_image.jpg", file_content, content_type="image/jpeg")
        
        asset1 = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file_1
        )
        asset2 = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file_2
        )
        
        import re
        self.assertTrue(re.match(r"^log_assets/230919-bear-[0-9a-f]{8}\.jpg$", asset1.file.name))
        self.assertTrue(re.match(r"^log_assets/230919-bear-[0-9a-f]{8}\.jpg$", asset2.file.name))

    @patch('threading.Thread')
    def test_create_log_asset_path_traversal(self, mock_thread):
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        file_content = b"fake image content"
        uploaded_file = SimpleUploadedFile("test_image.jpg", file_content, content_type="image/jpeg")
        
        asset = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file,
            custom_filename="../../test_path_traversal.jpg"
        )
        # Check that it is sanitized and stored inside log_assets folder
        self.assertEqual(os.path.basename(asset.file.name), "test_path_traversal.jpg")
        self.assertTrue(asset.file.name.startswith("log_assets/"))

    @patch('subprocess.run')
    def test_compress_asset_jpeg(self, mock_run):
        # Create models manually or run compression directly
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        asset = LogAsset(log_entry=entry)
        
        # Call compress_asset directly to avoid threading issues in tests
        asset.compress_asset("/path/to/test.jpg", ".jpg")
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn('mogrify', cmd)
        self.assertIn('800x450', cmd)
        self.assertNotIn('16:9', cmd)
        self.assertIn('/path/to/test.jpg', cmd)

    @patch('subprocess.run')
    def test_compress_asset_png_bypasses(self, mock_run):
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        asset = LogAsset(log_entry=entry)
        asset.compress_asset("/path/to/test.png", ".png")
        mock_run.assert_not_called()

    @patch('subprocess.run')
    @patch('os.path.exists')
    def test_compress_asset_mp4(self, mock_exists, mock_run):
        # We want to test that ffmpeg is called to create ogg, webm, and poster
        # Let's say all paths don't exist yet
        mock_exists.return_value = False
        
        entry = LogEntry.objects.create(
            title="230919-bear", 
            slug="230919-bear", 
            content_markdown="Content", 
            publish_date=timezone.now()
        )
        asset = LogAsset(log_entry=entry)
        
        asset.compress_asset("/path/to/video.mp4", ".mp4")
        
        # subprocess.run should be called for mp4, ogg, webm, and jpeg poster
        self.assertTrue(mock_run.call_count >= 3)
        called_cmds = [call_args[0][0] for call_args in mock_run.call_args_list]
        for cmd in called_cmds:
            self.assertIn('ffmpeg', cmd)
            # Find ogg and webm commands and verify they don't contain -preset or -movflags
            if any(arg.endswith('.ogg') for arg in cmd) or any(arg.endswith('.webm') for arg in cmd):
                self.assertNotIn('-preset', cmd)
                self.assertNotIn('-movflags', cmd)
