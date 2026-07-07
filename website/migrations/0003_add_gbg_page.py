import sys
from django.db import migrations

def add_gbg_page(apps, schema_editor):
    # Skip creating the fixture/page during unit testing to keep tests clean
    if 'test' in sys.argv:
        return
        
    Page = apps.get_model('website', 'Page')
    Page.objects.get_or_create(
        slug='gbg',
        defaults={
            'title': 'Gbg',
            'content_markdown': '[THE](eggs) [GLASS](2) [BEAD](archaic) [GAME](dash)'
        }
    )

class Migration(migrations.Migration):

    dependencies = [
        ('website', '0002_load_initial_data'),
    ]

    operations = [
        migrations.RunPython(add_gbg_page),
    ]
