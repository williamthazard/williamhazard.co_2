"""Mint the writer API token. Prints the token and its hash exactly once."""
import hashlib
import secrets

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a writer API token; put the hash in the server env, the token in yours."

    def handle(self, *args, **options):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.stdout.write("Shown once — store both now.")
        self.stdout.write(f"BLOG_WRITER_TOKEN={token}")
        self.stdout.write(f"WRITER_TOKEN_HASH={digest}")
