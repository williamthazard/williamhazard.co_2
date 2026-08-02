import sys
from django.db import migrations

def add_wrapped_2025_page(apps, schema_editor):
    if 'test' in sys.argv:
        return
        
    Page = apps.get_model('website', 'Page')
    Page.objects.get_or_create(
        slug='wrapped-2025',
        defaults={
            'title': 'on awakening (2025 playlist dream journal explorer)',
            'content_markdown': 'Dynamic 2025 waking playlist dream journal explorer and analytics.'
        }
    )

def remove_wrapped_2025_page(apps, schema_editor):
    if 'test' in sys.argv:
        return
    Page = apps.get_model('website', 'Page')
    Page.objects.filter(slug='wrapped-2025').delete()

class Migration(migrations.Migration):

    dependencies = [
        ('website', '0006_webmention'),
    ]

    operations = [
        migrations.RunPython(add_wrapped_2025_page, remove_wrapped_2025_page),
    ]
