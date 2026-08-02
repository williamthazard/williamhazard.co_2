from django.core.management.base import BaseCommand
from website.webmention_sync import sync_webmentions_from_api

class Command(BaseCommand):
    help = "Syncs webmentions from the webmention.io API."

    def add_arguments(self, parser):
        parser.add_argument('--token', type=str, help="webmention.io API token")
        parser.add_argument('--domain', type=str, help="Optional domain filter")

    def handle(self, *args, **options):
        token = options.get('token')
        domain = options.get('domain')
        self.stdout.write("Fetching webmentions from webmention.io API...")
        result = sync_webmentions_from_api(token=token, domain=domain)

        if result['status'] == 'error':
            self.stdout.write(self.style.ERROR(f"Error syncing webmentions: {result['message']}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Successfully synced webmentions! "
                f"Total fetched: {result['total_fetched']}, "
                f"Created: {result['created']}, "
                f"Updated: {result['updated']}"
            ))
