from django import template
from django.utils.safestring import mark_safe
import markdown as md

register = template.Library()

@register.filter(name='render_markdown')
def render_markdown(value):
    if not value:
        return ""
    html = md.markdown(value, extensions=[
        'markdown.extensions.fenced_code',
        'markdown.extensions.tables',
    ])
    # Log entries embed multi-megabyte originals and the index renders
    # ten entries per page, so defer offscreen image fetches.
    html = html.replace('<img ', '<img loading="lazy" decoding="async" ')
    return mark_safe(html)
