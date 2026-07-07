from django.test import TestCase, override_settings
from django.contrib import admin
from unittest.mock import patch
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
import tempfile
import shutil
from website.models import Page, LogEntry, LogAsset, PageAsset
from website.admin import PageAdmin, LogEntryAdmin, LogAssetInline, PageAssetInline

TEMP_MEDIA_ROOT = tempfile.mkdtemp()

@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class AdminTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # We need a superuser to access the admin client
        from django.contrib.auth.models import User
        cls.admin_user = User.objects.create_superuser(
            username='admin_test',
            email='admin_test@example.com',
            password='admin123'
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)

    def test_admin_registration(self):
        # Check models registered in admin site
        self.assertIn(Page, admin.site._registry)
        self.assertIn(LogEntry, admin.site._registry)
        self.assertIn(PageAsset, admin.site._registry)
        self.assertIn(LogAsset, admin.site._registry)
        
        # Verify classes
        self.assertIsInstance(admin.site._registry[Page], PageAdmin)
        self.assertIsInstance(admin.site._registry[LogEntry], LogEntryAdmin)

    def test_page_admin_config(self):
        page_admin = admin.site._registry[Page]
        self.assertEqual(page_admin.list_display, ('title', 'slug', 'updated_at'))
        self.assertEqual(page_admin.prepopulated_fields, {'slug': ('title',)})
        self.assertIn(LogAssetInline if False else PageAssetInline, page_admin.inlines)

    def test_log_entry_admin_config(self):
        log_admin = admin.site._registry[LogEntry]
        self.assertEqual(
            log_admin.list_display, 
            ('title', 'publish_date', 'posted_to_bluesky', 'posted_to_mastodon')
        )
        self.assertEqual(log_admin.search_fields, ('title', 'content_markdown'))
        self.assertEqual(log_admin.prepopulated_fields, {'slug': ('title',)})
        self.assertIn(LogAssetInline, log_admin.inlines)
        self.assertEqual(log_admin.readonly_fields, ('posted_to_bluesky', 'posted_to_mastodon'))
    @patch('threading.Thread')
    def test_log_asset_inline_config(self, mock_thread):
        inline = LogAssetInline(LogEntry, admin.site)
        self.assertEqual(inline.fields, ('file', 'custom_filename', 'copyable_snippet'))
        self.assertEqual(inline.readonly_fields, ('copyable_snippet',))
        
        # Test copyable_snippet return values
        # No file yet
        entry = LogEntry.objects.create(
            title="Test Entry",
            slug="test-entry",
            content_markdown="Text",
            publish_date=timezone.now()
        )
        asset = LogAsset(log_entry=entry)
        self.assertEqual(inline.copyable_snippet(asset), "Save model to see snippet")
        
        # With file
        uploaded_file = SimpleUploadedFile("test.jpg", b"fake content", content_type="image/jpeg")
        asset.file = uploaded_file
        asset.save()
        snippet = inline.copyable_snippet(asset)
        self.assertIn(asset.file.url, snippet)
        self.assertTrue(snippet.startswith('<code'))

    def test_admin_views_accessible(self):
        self.client.login(username='admin_test', password='admin123')
        
        # Admin dashboard
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        
        # Page list
        response = self.client.get(reverse('admin:website_page_changelist'))
        self.assertEqual(response.status_code, 200)

        # LogEntry list
        response = self.client.get(reverse('admin:website_logentry_changelist'))
        self.assertEqual(response.status_code, 200)
