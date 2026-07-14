import os
import shutil
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from django.contrib.staticfiles import finders
from website.models import Page, LogEntry

@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class ViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        Page.objects.create(title="Home", slug="home", content_markdown="Welcome to my homepage")
        Page.objects.create(title="Words", slug="words", content_markdown="My words list")
        LogEntry.objects.create(title="bear", slug="230919-bear", content_markdown="Bear post", publish_date=timezone.now())

    def test_home_view(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome")

    def test_static_assets_referenced_in_html(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/static/css/styles.css"')
        self.assertContains(response, 'href="/static/favicon.ico"')

    def test_static_files_exist_on_disk(self):
        css_path = finders.find('css/styles.css')
        self.assertIsNotNone(css_path)
        self.assertTrue(os.path.exists(css_path))

        favicon_path = finders.find('favicon.ico')
        self.assertIsNotNone(favicon_path)
        self.assertTrue(os.path.exists(favicon_path))

        font_path = finders.find('fonts/RobotoMono-VariableFont_wght.ttf')
        self.assertIsNotNone(font_path)
        self.assertTrue(os.path.exists(font_path))

    def test_page_view(self):
        response = self.client.get(reverse('page_detail', kwargs={'page_slug': 'words'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My words list")

    def test_log_index(self):
        response = self.client.get(reverse('log_index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bear")

    def test_log_detail(self):
        response = self.client.get(reverse('log_detail', kwargs={'entry_slug': '230919-bear'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bear post")

    def test_rss_feed(self):
        response = self.client.get(reverse('rss_feed'))
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/rss+xml", response.headers.get("Content-Type", ""))
        self.assertContains(response, "log / bear")
        self.assertContains(response, "Bear post")

    def test_serve_sketches(self):
        # Create sketches directory and a dummy file
        sketches_dir = os.path.join(settings.BASE_DIR, 'sketches')
        os.makedirs(sketches_dir, exist_ok=True)
        
        test_dir = os.path.join(sketches_dir, 'test_sketch')
        os.makedirs(test_dir, exist_ok=True)
        
        dummy_file = os.path.join(test_dir, 'index.html')
        with open(dummy_file, 'w') as f:
            f.write("Hello Sketch")
            
        try:
            # Test direct file access
            response = self.client.get(reverse('serve_sketches', kwargs={'path': 'test_sketch/index.html'}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"Hello Sketch")
            
            # Test directory fallback to index.html with trailing slash
            response = self.client.get(reverse('serve_sketches', kwargs={'path': 'test_sketch/'}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"Hello Sketch")
            
            # Test directory redirect without trailing slash
            response = self.client.get(reverse('serve_sketches', kwargs={'path': 'test_sketch'}))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, '/sketches/test_sketch/')
            
            # Test 404 for non-existent file
            response = self.client.get(reverse('serve_sketches', kwargs={'path': 'non_existent.html'}))
            self.assertEqual(response.status_code, 404)
        finally:
            # Clean up
            if os.path.exists(test_dir):
                shutil.rmtree(test_dir)


