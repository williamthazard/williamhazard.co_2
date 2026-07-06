import os
import shutil
import tempfile
from django.test import TestCase, override_settings
from django.core.management import call_command
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
        with open(os.path.join(cls.temp_repo, 'log', 'entries', '230919-bear.md'), 'w') as f:
            f.write("A bear is here. ![](pics/bear.jpeg)\n[audio](audio/bear.mp3)")
        with open(os.path.join(cls.temp_repo, 'log', 'entries', 'pics', 'bear.jpeg'), 'w') as f:
            f.write("bear-image-data")
            
        # 3. Sketches/GBG directories
        os.makedirs(os.path.join(cls.temp_repo, 'sketches', 'mock_sketch'))
        with open(os.path.join(cls.temp_repo, 'sketches', 'mock_sketch', 'index.html'), 'w') as f:
            f.write("Sketch HTML")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_media, ignore_errors=True)
        shutil.rmtree(cls.temp_repo, ignore_errors=True)
        super().tearDownClass()

    def test_migration_command_imports_everything(self):
        # Override media settings and execute the import command pointing to mock repo
        with override_settings(MEDIA_ROOT=self.temp_media):
            call_command('import_existing_site', repo_path=self.temp_repo)
            
            # 1. Verify Pages created
            home_page = Page.objects.get(slug='home')
            self.assertIn("Welcome home", home_page.content_markdown)
            self.assertIn("/media/page_assets/header.jpeg", home_page.content_markdown)
            
            words_page = Page.objects.get(slug='words')
            self.assertIn("Words list", words_page.content_markdown)
            self.assertIn("/media/page_assets/words.jpeg", words_page.content_markdown)
            
            # 2. Verify Log Entries created with correct date and status flags
            entry = LogEntry.objects.get(slug='230919-bear')
            self.assertEqual(entry.title, 'bear')
            self.assertEqual(entry.publish_date.year, 2023)
            self.assertEqual(entry.publish_date.month, 9)
            self.assertEqual(entry.publish_date.day, 19)
            self.assertIn("/media/log_assets/bear.jpeg", entry.content_markdown)
            
            # Social sharing flags must be marked True to prevent reposts
            self.assertTrue(entry.posted_to_bluesky)
            self.assertTrue(entry.posted_to_mastodon)
            
            # Verify LogAsset is registered
            self.assertEqual(entry.assets.count(), 1)
            asset = entry.assets.first()
            self.assertIn("bear.jpeg", asset.file.name)
            
            # 3. Verify media files exist on disk in destination
            self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'page_assets', 'header.jpeg')))
            self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'page_assets', 'words.jpeg')))
            self.assertTrue(os.path.exists(os.path.join(self.temp_media, 'log_assets', 'bear.jpeg')))
