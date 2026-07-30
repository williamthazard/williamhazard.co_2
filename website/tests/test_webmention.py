import json
import os
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from website.models import LogEntry, Webmention

class WebmentionTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.log_entry = LogEntry.objects.create(
            title="test-post",
            slug="240809-test-post",
            content_markdown="This is a test post.",
            publish_date=timezone.now()
        )

    def test_webmention_model_str(self):
        wm = Webmention.objects.create(
            target_url="https://williamhazard.co/log/240809-test-post/",
            source_url="https://example.com/reply",
            author_name="Alice",
            comment_type="reply"
        )
        self.assertIn("reply from Alice", str(wm))

    def test_webhook_get_request_rejected(self):
        response = self.client.get(reverse('webmention_webhook'))
        self.assertEqual(response.status_code, 400)

    def test_webhook_creates_webmention_and_matches_log_entry(self):
        payload = {
            "source": "https://indieweb.example/post/1",
            "target": "https://williamhazard.co/log/240809-test-post/",
            "post": {
                "type": "entry",
                "name": "Nice post!",
                "url": "https://indieweb.example/post/1",
                "published": "2026-07-30T12:00:00Z",
                "wm-id": 998877,
                "wm-property": "in-reply-to",
                "author": {
                    "name": "Bob Programmer",
                    "photo": "https://indieweb.example/avatar.jpg",
                    "url": "https://indieweb.example"
                },
                "content": {
                    "html": "<p>Loved reading this post!</p>",
                    "text": "Loved reading this post!"
                }
            }
        }

        response = self.client.post(
            reverse('webmention_webhook'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['created'])
        self.assertEqual(data['matched_log_entry'], '240809-test-post')

        wm = Webmention.objects.get(wm_id=998877)
        self.assertEqual(wm.author_name, 'Bob Programmer')
        self.assertEqual(wm.comment_type, 'reply')
        self.assertEqual(wm.log_entry, self.log_entry)
        self.assertIn("Loved reading this post!", wm.content_html)

    @patch.dict(os.environ, {"WEBMENTION_IO_SECRET": "secret_token_123"})
    def test_webhook_secret_authentication(self):
        payload = {
            "source": "https://example.com/src",
            "target": "https://williamhazard.co/log/240809-test-post/"
        }
        
        # Missing secret -> 403
        res1 = self.client.post(
            reverse('webmention_webhook'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res1.status_code, 403)

        # Invalid secret -> 403
        res2 = self.client.post(
            reverse('webmention_webhook') + "?secret=wrong_secret",
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, 403)

        # Valid secret query param -> 200
        res3 = self.client.post(
            reverse('webmention_webhook') + "?secret=secret_token_123",
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res3.status_code, 200)

    def test_log_detail_view_renders_approved_webmentions(self):
        Webmention.objects.create(
            log_entry=self.log_entry,
            target_url=f"https://williamhazard.co/log/{self.log_entry.slug}/",
            source_url="https://example.org/blog/comment-1",
            author_name="Charlie Reader",
            author_photo="https://example.org/charlie.jpg",
            author_url="https://example.org",
            comment_type="reply",
            content_html="<p>Awesome insight into poetry and hazard!</p>",
            is_approved=True
        )

        response = self.client.get(reverse('log_detail', kwargs={'entry_slug': self.log_entry.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Charlie Reader")
        self.assertContains(response, "Awesome insight into poetry and hazard!")
        self.assertContains(response, "https://example.org/charlie.jpg")
        self.assertContains(response, "h-entry")
        self.assertContains(response, "p-name u-url")
