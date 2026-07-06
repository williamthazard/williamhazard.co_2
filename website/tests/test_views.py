import os
import shutil
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from website.models import Page, LogEntry

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

    def test_serve_gbg(self):
        # Create gbg directory and a dummy file
        gbg_dir = os.path.join(settings.BASE_DIR, 'gbg')
        os.makedirs(gbg_dir, exist_ok=True)
        
        test_dir = os.path.join(gbg_dir, 'test_gbg')
        os.makedirs(test_dir, exist_ok=True)
        
        dummy_file = os.path.join(test_dir, 'index.html')
        with open(dummy_file, 'w') as f:
            f.write("Hello GBG")
            
        # Create a symlink pointing outside the gbg directory
        outside_file = os.path.abspath(os.path.join(settings.BASE_DIR, 'website', 'models.py'))
        symlink_path = os.path.join(test_dir, 'sym_models.py')
        if not os.path.exists(symlink_path):
            os.symlink(outside_file, symlink_path)
            
        try:
            # Test direct file access
            response = self.client.get(reverse('serve_gbg', kwargs={'path': 'test_gbg/index.html'}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"Hello GBG")
            
            # Test directory fallback to index.html
            response = self.client.get(reverse('serve_gbg', kwargs={'path': 'test_gbg/'}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"Hello GBG")
            
            # Test 404 for non-existent file
            response = self.client.get(reverse('serve_gbg', kwargs={'path': 'non_existent.html'}))
            self.assertEqual(response.status_code, 404)
            
            # Test directory traversal prevention via parent directory (..)
            response = self.client.get(reverse('serve_gbg', kwargs={'path': '../website/models.py'}))
            self.assertEqual(response.status_code, 404)
            
            # Test directory traversal prevention via symbolic link (resolved to outside base_dir)
            response = self.client.get(reverse('serve_gbg', kwargs={'path': 'test_gbg/sym_models.py'}))
            self.assertEqual(response.status_code, 404)
        finally:
            # Clean up
            if os.path.exists(test_dir):
                shutil.rmtree(test_dir)

