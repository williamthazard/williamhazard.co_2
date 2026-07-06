import os
import tempfile
import shutil
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch, MagicMock
from website.models import LogEntry, LogAsset
from website.signals import post_to_bluesky, post_to_mastodon

# Create a temporary directory for media files during tests
TEMP_MEDIA_ROOT = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class SignalsTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

    @patch('website.signals.transaction.on_commit')
    @patch('website.signals.post_to_bluesky')
    @patch('website.signals.post_to_mastodon')
    @patch('threading.Thread')
    def test_social_posting_signals(self, mock_thread, mock_mastodon, mock_bluesky, mock_on_commit):
        # Configure mock_on_commit to run the callback immediately in tests
        mock_on_commit.side_effect = lambda fn: fn()

        # Configure the thread mock to run the target function synchronously
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance
        def start_side_effect():
            target = mock_thread.call_args[1]['target']
            target()
        mock_thread_instance.start.side_effect = start_side_effect

        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown="This is a test",
            publish_date=timezone.now(),
            share_to_bluesky=True,
            share_to_mastodon=True
        )
        # Verify that transaction.on_commit was called
        mock_on_commit.assert_called_once()

        # Verify that threading.Thread was called
        mock_thread.assert_called_once()
        
        # Signals should trigger mock posts
        mock_bluesky.assert_called_once_with(entry)
        mock_mastodon.assert_called_once_with(entry)
        
        # Refresh from DB
        entry.refresh_from_db()
        self.assertTrue(entry.posted_to_bluesky)
        self.assertTrue(entry.posted_to_mastodon)

    @patch('requests.post')
    @patch.dict(os.environ, {}, clear=True)
    def test_post_to_bluesky_missing_credentials(self, mock_post):
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown="This is a test",
            publish_date=timezone.now()
        )
        post_to_bluesky(entry)
        mock_post.assert_not_called()

    @patch('requests.post')
    @patch.dict(os.environ, {}, clear=True)
    def test_post_to_mastodon_missing_credentials(self, mock_post):
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown="This is a test",
            publish_date=timezone.now()
        )
        post_to_mastodon(entry)
        mock_post.assert_not_called()

    @patch('requests.post')
    @patch('threading.Thread')
    @patch.dict(os.environ, {
        'BLUESKY_HANDLE': 'test.bsky.social',
        'BLUESKY_PASSWORD': 'password123',
        'BLUESKY_PDS_URL': 'https://bsky.social'
    })
    def test_post_to_bluesky_with_image_asset(self, mock_thread, mock_post):
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown="This is a [link](https://example.com) test.",
            publish_date=timezone.now()
        )
        file_content = b"fake image content"
        uploaded_file = SimpleUploadedFile("test_image.jpg", file_content, content_type="image/jpeg")
        asset = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file
        )

        # Mock responses for createSession, uploadBlob, createRecord
        mock_session_res = MagicMock()
        mock_session_res.status_code = 200
        mock_session_res.json.return_value = {
            'accessJwt': 'fake-jwt',
            'did': 'did:plc:fake-did'
        }

        mock_blob_res = MagicMock()
        mock_blob_res.status_code = 200
        mock_blob_res.json.return_value = {
            'blob': 'fake-blob-ref'
        }

        mock_record_res = MagicMock()
        mock_record_res.status_code = 200

        mock_post.side_effect = [mock_session_res, mock_blob_res, mock_record_res]

        post_to_bluesky(entry)

        # 3 calls: session, uploadBlob, createRecord
        self.assertEqual(mock_post.call_count, 3)

        # Verify session call
        session_args, session_kwargs = mock_post.call_args_list[0]
        self.assertEqual(session_args[0], 'https://bsky.social/xrpc/com.atproto.server.createSession')
        self.assertEqual(session_kwargs['json'], {'identifier': 'test.bsky.social', 'password': 'password123'})
        self.assertEqual(session_kwargs.get('timeout'), 15)

        # Verify uploadBlob call
        upload_args, upload_kwargs = mock_post.call_args_list[1]
        self.assertEqual(upload_args[0], 'https://bsky.social/xrpc/com.atproto.repo.uploadBlob')
        self.assertEqual(upload_kwargs['headers']['Authorization'], 'Bearer fake-jwt')
        self.assertEqual(upload_kwargs.get('timeout'), 15)

        # Verify createRecord call
        record_args, record_kwargs = mock_post.call_args_list[2]
        self.assertEqual(record_args[0], 'https://bsky.social/xrpc/com.atproto.repo.createRecord')
        self.assertEqual(record_kwargs['headers']['Authorization'], 'Bearer fake-jwt')
        self.assertEqual(record_kwargs.get('timeout'), 15)
        
        # Verify text cleaning (markdown link stripped to display text)
        post_record = record_kwargs['json']['record']
        self.assertEqual(post_record['text'], 'This is a link test.')
        self.assertEqual(post_record['embed']['images'][0]['image'], 'fake-blob-ref')

    @patch('requests.post')
    @patch('threading.Thread')
    @patch.dict(os.environ, {
        'MASTODON_ACCESS_TOKEN': 'token123',
        'MASTODON_API_BASE_URL': 'https://mastodon.social'
    })
    def test_post_to_mastodon_with_media_asset(self, mock_thread, mock_post):
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown="This is a [link](https://example.com) test.",
            publish_date=timezone.now()
        )
        file_content = b"fake image content"
        uploaded_file = SimpleUploadedFile("test_image.jpg", file_content, content_type="image/jpeg")
        asset = LogAsset.objects.create(
            log_entry=entry,
            file=uploaded_file
        )

        mock_media_res = MagicMock()
        mock_media_res.status_code = 200
        mock_media_res.json.return_value = {'id': 'media-id-123'}

        mock_status_res = MagicMock()
        mock_status_res.status_code = 200

        mock_post.side_effect = [mock_media_res, mock_status_res]

        post_to_mastodon(entry)

        self.assertEqual(mock_post.call_count, 2)

        # Verify upload call
        upload_args, upload_kwargs = mock_post.call_args_list[0]
        self.assertEqual(upload_args[0], 'https://mastodon.social/api/v1/media')
        self.assertEqual(upload_kwargs['headers']['Authorization'], 'Bearer token123')
        self.assertEqual(upload_kwargs.get('timeout'), 15)

        # Verify status call
        status_args, status_kwargs = mock_post.call_args_list[1]
        self.assertEqual(status_args[0], 'https://mastodon.social/api/v1/statuses')
        self.assertEqual(status_kwargs['headers']['Authorization'], 'Bearer token123')
        self.assertEqual(status_kwargs.get('timeout'), 15)
        self.assertEqual(status_kwargs['data']['media_ids[]'], ['media-id-123'])
        
        # Verify text cleaning and read more link
        self.assertIn('This is a link test.', status_kwargs['data']['status'])
        self.assertIn('Read more: https://williamhazard.co/log/230919-test/', status_kwargs['data']['status'])

    @patch('requests.post')
    @patch.dict(os.environ, {
        'BLUESKY_HANDLE': 'test.bsky.social',
        'BLUESKY_PASSWORD': 'password123',
        'BLUESKY_PDS_URL': 'https://bsky.social'
    })
    def test_bluesky_truncation(self, mock_post):
        # 300 char content
        long_content = "a" * 300
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown=long_content,
            publish_date=timezone.now()
        )

        mock_session_res = MagicMock()
        mock_session_res.status_code = 200
        mock_session_res.json.return_value = {
            'accessJwt': 'fake-jwt',
            'did': 'did:plc:fake-did'
        }
        mock_record_res = MagicMock()
        mock_record_res.status_code = 200

        mock_post.side_effect = [mock_session_res, mock_record_res]

        post_to_bluesky(entry)

        # The post text should be truncated to 277 chars + "..." (280 chars total)
        record_kwargs = mock_post.call_args_list[1][1]
        post_text = record_kwargs['json']['record']['text']
        self.assertEqual(len(post_text), 280)
        self.assertTrue(post_text.endswith("..."))

    @patch('requests.post')
    @patch.dict(os.environ, {
        'MASTODON_ACCESS_TOKEN': 'token123',
        'MASTODON_API_BASE_URL': 'https://mastodon.social'
    })
    def test_mastodon_truncation(self, mock_post):
        # Mastodon limit is 500 including the read more link (approx 50 chars).
        # We'll make the content 500 chars to force truncation
        long_content = "a" * 500
        entry = LogEntry.objects.create(
            title="Test Post",
            slug="230919-test",
            content_markdown=long_content,
            publish_date=timezone.now()
        )

        mock_status_res = MagicMock()
        mock_status_res.status_code = 200
        mock_post.return_value = mock_status_res

        post_to_mastodon(entry)

        status_kwargs = mock_post.call_args_list[0][1]
        status_text = status_kwargs['data']['status']
        self.assertEqual(len(status_text), 500)
        self.assertTrue(status_text.startswith("a"))
        self.assertIn("...", status_text)
        self.assertIn("Read more: https://williamhazard.co/log/230919-test/", status_text)
