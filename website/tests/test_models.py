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
        
        self.assertTrue(asset1.file.name.endswith("230919-bear-1.jpg"))
        self.assertTrue(asset2.file.name.endswith("230919-bear-2.jpg"))

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
        self.assertIn('/path/to/test.jpg', cmd)

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
        
        # subprocess.run should be called for ogg, webm, and jpeg poster
        # and maybe mp4 if not exists? In this case .mp4 file exists (input), but target is webm/ogg/jpeg.
        # Let's verify ffmpeg calls
        self.assertTrue(mock_run.call_count >= 3)
        called_cmds = [call_args[0][0] for call_args in mock_run.call_args_list]
        for cmd in called_cmds:
            self.assertIn('ffmpeg', cmd)
