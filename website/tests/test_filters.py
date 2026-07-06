from django.test import TestCase
from website.templatetags.markdown_filters import render_markdown

class FilterTestCase(TestCase):
    def test_render_simple_markdown(self):
        md_text = "Hello *world*"
        html = render_markdown(md_text)
        self.assertEqual(html, "<p>Hello <em>world</em></p>")

    def test_render_raw_html_intact(self):
        md_text = '<video src="test.mp4"></video>'
        html = render_markdown(md_text)
        self.assertIn('<video src="test.mp4"></video>', html)

    def test_render_none_value(self):
        html = render_markdown(None)
        self.assertEqual(html, "")

    def test_render_empty_string(self):
        html = render_markdown("")
        self.assertEqual(html, "")

    def test_render_fenced_code(self):
        md_text = "```python\nprint('hello')\n```"
        html = render_markdown(md_text)
        self.assertIn("<pre><code class=\"language-python\">", html)
        self.assertIn("print('hello')", html)

    def test_render_table(self):
        md_text = "| Header 1 | Header 2 |\n| --- | --- |\n| Cell 1 | Cell 2 |"
        html = render_markdown(md_text)
        self.assertIn("<table>", html)
        self.assertIn("<thead>", html)
        self.assertIn("<tbody>", html)
        self.assertIn("Cell 1", html)
