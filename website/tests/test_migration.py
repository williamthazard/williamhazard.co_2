from django.test import TestCase
from website.models import Page

class MigrationTestCase(TestCase):
    def test_initial_pages_exist(self):
        page = Page.objects.create(title="Home", slug="home", content_markdown="Welcome")
        self.assertEqual(page.slug, "home")
