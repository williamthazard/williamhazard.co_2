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

    def test_images_get_lazy_loading_attributes(self):
        html = render_markdown('![a photo](/media/log_assets/250708.png)')
        self.assertIn('loading="lazy"', html)
        self.assertIn('decoding="async"', html)

    def test_non_image_content_untouched(self):
        html = render_markdown('Hello *world*')
        self.assertNotIn('loading="lazy"', html)
