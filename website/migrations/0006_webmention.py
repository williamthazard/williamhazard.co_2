from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    dependencies = [
        ('website', '0005_delete_gbg_page'),
    ]

    operations = [
        migrations.CreateModel(
            name='Webmention',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_url', models.URLField(max_length=500)),
                ('target_url', models.URLField(max_length=500)),
                ('author_name', models.CharField(blank=True, max_length=200)),
                ('author_photo', models.URLField(blank=True, max_length=500)),
                ('author_url', models.URLField(blank=True, max_length=500)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('wm_id', models.BigIntegerField(unique=True)),
                ('wm_property', models.CharField(blank=True, max_length=50)),
                ('content_text', models.TextField(blank=True)),
                ('content_html', models.TextField(blank=True)),
                ('log_entry', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='webmentions', to='website.logentry')),
                ('page', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='webmentions', to='website.page')),
            ],
            options={
                'ordering': ['-published_at', '-received_at'],
            },
        ),
    ]
