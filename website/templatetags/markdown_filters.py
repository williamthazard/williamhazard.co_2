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
    return mark_safe(html)
