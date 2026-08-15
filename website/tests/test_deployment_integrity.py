import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings


FIXTURE = Path(settings.BASE_DIR) / 'website' / 'fixtures' / 'website_fixture.json'


class FixtureMediaIntegrityTests(TestCase):
    """Guard against fixture content referencing files absent from the repo.

    These pass trivially on a machine where the files exist but fail on a
    fresh clone (e.g. Render's build) if a referenced asset was never
    committed, which is exactly the failure mode they exist to catch.
    """

    def test_all_referenced_media_files_exist_on_disk(self):
        refs = sorted(set(re.findall(r'media/[A-Za-z0-9_/.-]+', FIXTURE.read_text())))
        missing = [r for r in refs if not (Path(settings.BASE_DIR) / r).is_file()]
        self.assertEqual(missing, [], f"fixture references missing media files: {missing}")

    def test_all_referenced_sketch_pages_exist_on_disk(self):
        refs = sorted(set(re.findall(r'sketches/[A-Za-z0-9_-]+', FIXTURE.read_text())))
        missing = [r for r in refs if not (Path(settings.BASE_DIR) / r / 'index.html').is_file()]
        self.assertEqual(missing, [], f"fixture links sketch pages without an index.html: {missing}")


@override_settings(DEBUG=False)
class ServeMediaTests(TestCase):
    def test_media_served_when_debug_false(self):
        resp = self.client.get('/media/log_assets/fata-morgana.mp3')
        self.assertEqual(resp.status_code, 200)
        resp.close()
