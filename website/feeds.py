from django.contrib.syndication.views import Feed
from django.urls import reverse
from .models import LogEntry
import markdown as md

class LatestLogFeed(Feed):
    title = "log"
    link = "/log/"
    description = "Log entries and poetry updates from William Hazard."

    def items(self):
        return LogEntry.objects.all()[:20]

    def item_title(self, item):
        return f"log / {item.title}"

    def item_description(self, item):
        # Render markdown to HTML for RSS readers
        return md.markdown(item.content_markdown, extensions=[
            'markdown.extensions.fenced_code',
            'markdown.extensions.tables',
        ])

    def item_link(self, item):
        return reverse('log_detail', args=[item.slug])

    def item_pubdate(self, item):
        return item.publish_date
