from django.test import TestCase
from website.templatetags.markdown_filters import render_markdown

class FilterTestCase(TestCase):
    def test_render_simple_markdown(self):
        md_text = "Hello *world*"
        html = render_markdown(md_text)
        self.assertEqual(html, "<p>Hello <em>world</em></p>")

    def test_render_none_value(self):
        html = render_markdown(None)
        self.assertEqual(html, "")
