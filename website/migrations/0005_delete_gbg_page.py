from django.db import migrations

def delete_gbg_page(apps, schema_editor):
    Page = apps.get_model('website', 'Page')
    Page.objects.filter(slug='gbg').delete()

class Migration(migrations.Migration):

    dependencies = [
        ('website', '0004_pageasset'),
    ]

    operations = [
        migrations.RunPython(delete_gbg_page, reverse_code=migrations.RunPython.noop),
    ]
