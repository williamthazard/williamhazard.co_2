import os
import shutil
import tempfile
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.conf import settings
from website.models import Page, LogEntry, LogAsset

class MigrationTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temp_media = tempfile.mkdtemp()
        cls.temp_repo = tempfile.mkdtemp()
        
        # Create a mock williamhazard.co repository structure
        # 1. Pages
        os.makedirs(os.path.join(cls.temp_repo, 'words'))
        os.makedirs(os.path.join(cls.temp_repo, 'sketches'))
        
        with open(os.path.join(cls.temp_repo, 'index.md'), 'w') as f:
            f.write("Welcome home ![](header.jpeg)")
        with open(os.path.join(cls.temp_repo, 'header.jpeg'), 'w') as f:
            f.write("header-data")
            
        with open(os.path.join(cls.temp_repo, 'words', 'index.md'), 'w') as f:
            f.write("Words list ![](words.jpeg)")
        with open(os.path.join(cls.temp_repo, 'words', 'words.jpeg'), 'w') as f:
            f.write("words-image-data")
            
        with open(os.path.join(cls.temp_repo, 'sketches', 'index.md'), 'w') as f:
            f.write("Sketches list")

        # 2. Logs
        os.makedirs(os.path.join(cls.temp_repo, 'log', 'entries', 'pics'))
        os.makedirs(os.path.join(cls.temp_repo, 'log', 'audio'))
        
        with open(os.path.join(cls.temp_repo, 'log', 'entries', '230919-bear.md'), 'w') as f:
            f.write("A bear is here. ![bear](pics/bear.jpeg 'my bear title')\n[audio](audio/bear.mp3)")
            
        with open(os.path.join(cls.temp_repo, 'log', 'entries', 'pics', 'bear.jpeg'), 'w') as f:
            f.write("bear-image-data")
            
        with open(os.path.join(cls.temp_repo, 'log', 'audio', 'bear.mp3'), 'w') as f:
            f.write("bear-audio-data")
            
        # 3. Sketches/GBG directories
        os.makedirs(os.path.join(cls.temp_repo, 'sketches', 'mock_sketch'))
        with open(os.path.join(cls.temp_repo, 'sketches', 'mock_sketch', 'index.html'), 'w') as f:
            f.write("Sketch HTML")
            
        # Add root index.html to ensure it gets ignored
        with open(os.path.join(cls.temp_repo, 'sketches', 'index.html'), 'w') as f:
            f.write("Root index HTML")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_media, ignore_errors=True)
        shutil.rmtree(cls.temp_repo, ignore_errors=True)
        super().tearDownClass()

    def test_migration_command_imports_everything(self):
        # Override media settings and BASE_DIR to prevent workspace pollution
        self.temp_base = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=self.temp_media, BASE_DIR=self.temp_base):
                call_command('import_existing_site', repo_path=self.temp_repo)
                
                # 1. Verify Pages created
                home_page = Page.objects.get(slug='home')
                self.assertIn("Welcome home", home_page.content_markdown)
                self.assertIn("/media/page_assets/header.jpeg", home_page.content_markdown)
                self.assertEqual(home_page.assets.count(), 1)
                self.assertEqual(Page.objects.get(slug='words').assets.count(), 1)
                
                # 2. Verify Log Entries created with correct date and status flags
                entry = LogEntry.objects.get(slug='230919-bear')
                self.assertEqual(entry.title, 'bear')
                self.assertEqual(entry.publish_date.year, 2023)
                self.assertEqual(entry.publish_date.month, 9)
                self.assertEqual(entry.publish_date.day, 19)
                
                # Assert correct path replacements including title support and no path corruption
                self.assertEqual(
                    entry.content_markdown,
                    "A bear is here. ![bear](/media/log_assets/bear.jpeg 'my bear title')\n[audio](/media/log_assets/bear.mp3)"
                )
                
                # Social sharing flags must be marked True to prevent reposts
                self.assertTrue(entry.posted_to_bluesky)
                self.assertTrue(entry.posted_to_mastodon)
                
                # Verify LogAsset is registered
                self.assertEqual(entry.assets.count(), 2)
                
                # 3. Verify media files exist on disk in destination
                self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'page_assets', 'header.jpeg')))
                self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'log_assets', 'bear.jpeg')))
                self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'log_assets', 'bear.mp3')))
                
                # 4. Verify Sketches folder structure
                sketches_dest = os.path.join(self.temp_base, 'sketches')
                self.assertTrue(os.path.exists(sketches_dest))
                
                # Nested index.html must be preserved
                nested_html = os.path.join(sketches_dest, 'mock_sketch', 'index.html')
                self.assertTrue(os.path.exists(nested_html))
                with open(nested_html, 'r') as f:
                    self.assertEqual(f.read(), "Sketch HTML")
                    
                # Root index.html must be ignored (ignored via ignore_top_level_indexes)
                root_html = os.path.join(sketches_dest, 'index.html')
                self.assertFalse(os.path.exists(root_html))
        finally:
            shutil.rmtree(self.temp_base, ignore_errors=True)
