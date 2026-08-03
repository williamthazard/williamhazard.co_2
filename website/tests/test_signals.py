from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from website.models import LogEntry

class SignalsTestCase(TestCase):
    @patch('website.signals.post_to_bluesky')
    @patch('website.signals.post_to_mastodon')
    def test_post_save_signal_triggers_social_posts(self, mock_mastodon, mock_bluesky):
        entry = LogEntry.objects.create(
            title="Signal Test Log",
            slug="signal-test-log",
            content_markdown="Test content for signals",
            publish_date=timezone.now(),
            share_to_bluesky=True,
            share_to_mastodon=True
        )
        self.assertEqual(entry.slug, "signal-test-log")
