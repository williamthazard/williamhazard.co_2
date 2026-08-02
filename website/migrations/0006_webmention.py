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
                ('target_url', models.URLField(help_text='Target URL on this site.', max_length=500)),
                ('source_url', models.URLField(help_text='Source URL linking to target.', max_length=500)),
                ('wm_id', models.IntegerField(blank=True, help_text='Webmention.io internal ID.', null=True, unique=True)),
                ('comment_type', models.CharField(default='reply', help_text='Type: reply, like, repost, mention.', max_length=50)),
                ('author_name', models.CharField(blank=True, help_text='Author display name.', max_length=200)),
                ('author_photo', models.URLField(blank=True, help_text='Author avatar image URL.', max_length=500)),
                ('author_url', models.URLField(blank=True, help_text='Author homepage/profile URL.', max_length=500)),
                ('content_html', models.TextField(blank=True, help_text='Rendered HTML content of the mention.')),
                ('content_text', models.TextField(blank=True, help_text='Plain text content of the mention.')),
                ('published_at', models.DateTimeField(blank=True, help_text='Publication date of the mention.', null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True, help_text='Timestamp when received by our webhook.')),
                ('is_approved', models.BooleanField(default=True, help_text='Approval flag for displaying on the site.')),
                ('log_entry', models.ForeignKey(blank=True, help_text='Associated log entry post, if matched by target URL.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='webmentions', to='website.logentry')),
            ],
            options={
                'verbose_name': 'Webmention',
                'verbose_name_plural': 'Webmentions',
                'ordering': ['-published_at', '-received_at'],
            },
        ),
    ]
