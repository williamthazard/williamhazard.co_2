from django.core.management import call_command
from django.db import migrations

import sys

def load_fixture(apps, schema_editor):
    if 'test' in sys.argv:
        return
    call_command('loaddata', 'website_fixture.json', app_label='website')

def unload_fixture(apps, schema_editor):
    Page = apps.get_model('website', 'Page')
    LogEntry = apps.get_model('website', 'LogEntry')
    LogAsset = apps.get_model('website', 'LogAsset')
    Page.objects.all().delete()
    LogEntry.objects.all().delete()
    LogAsset.objects.all().delete()

class Migration(migrations.Migration):

    dependencies = [
        ('website', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(load_fixture, reverse_code=unload_fixture),
    ]
