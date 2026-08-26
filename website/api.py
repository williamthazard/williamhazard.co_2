"""Writer API: bearer-token endpoints for the log writer TUI."""
import functools
import hashlib
import hmac
import os

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def writer_token_required(view):
    @csrf_exempt
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        stored = os.environ.get("WRITER_TOKEN_HASH", "")
        if not stored:
            if settings.DEBUG:
                return view(request, *args, **kwargs)
            return JsonResponse({"error": "unauthorized"}, status=401)
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(digest, stored):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return view(request, *args, **kwargs)
    return wrapped


@writer_token_required
def ping(request):
    return JsonResponse({"ok": True})


import json as jsonlib

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import LogEntry, LogAsset


@writer_token_required
def entries(request):
    rows = LogEntry.objects.order_by("-publish_date").values("slug", "title", "publish_date")
    return JsonResponse({"entries": [
        {"slug": r["slug"], "title": r["title"], "publish_date": r["publish_date"].isoformat()}
        for r in rows
    ]})


def _entry_payload(e):
    return {
        "slug": e.slug, "title": e.title, "content_markdown": e.content_markdown,
        "publish_date": e.publish_date.isoformat(),
        "share_to_bluesky": e.share_to_bluesky, "share_to_mastodon": e.share_to_mastodon,
        "posted_to_bluesky": e.posted_to_bluesky, "posted_to_mastodon": e.posted_to_mastodon,
    }


@writer_token_required
def entry(request, slug):
    if request.method == "PUT":
        try:
            body = jsonlib.loads(request.body)
        except jsonlib.JSONDecodeError:
            return JsonResponse({"error": "invalid json"}, status=400)
        title = body.get("title")
        content = body.get("content_markdown")
        if not title or content is None:
            return JsonResponse({"error": "title and content_markdown are required"}, status=400)
        date = parse_datetime(body["publish_date"]) if body.get("publish_date") else None
        e = LogEntry.objects.filter(slug=slug).first()
        created = e is None
        if created:
            e = LogEntry(slug=slug, publish_date=date or timezone.now())
        elif date:
            e.publish_date = date
        e.title = title
        e.content_markdown = content
        if body.get("share_to_bluesky"):
            e.share_to_bluesky = True     # OR-on only; never flipped off here
        if body.get("share_to_mastodon"):
            e.share_to_mastodon = True
        e.save()
        return JsonResponse({"status": "created" if created else "updated", "slug": e.slug},
                            status=201 if created else 200)
    e = get_object_or_404(LogEntry, slug=slug)
    return JsonResponse(_entry_payload(e))


def _sha256_of(fileobj):
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(8192), b""):
        digest.update(chunk)
    return digest.hexdigest()


@writer_token_required
def assets(request):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    slug = request.POST.get("slug")
    log_entry = LogEntry.objects.filter(slug=slug).first() if slug else None
    if log_entry is None:
        return JsonResponse({"error": "unknown entry"}, status=404)

    name = request.POST.get("name")
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)

    uploaded = request.FILES.get("file")
    if uploaded is None:
        return JsonResponse({"error": "file is required"}, status=400)

    incoming_digest = _sha256_of(uploaded)
    uploaded.seek(0)

    existing = next(
        (a for a in log_entry.assets.all() if os.path.basename(a.file.name) == name),
        None,
    )
    if existing is not None:
        with existing.file.open("rb") as f:
            existing_digest = _sha256_of(f)
        if existing_digest == incoming_digest:
            return JsonResponse({"status": "unchanged"}, status=200)
        return JsonResponse({
            "status": "conflict",
            "error": (
                f"an asset named '{name}' already exists on this entry with different "
                "content; upload under a different name or remove the existing asset first"
            ),
        }, status=409)

    asset = LogAsset(log_entry=log_entry, custom_filename=name, file=uploaded)
    asset.save()
    return JsonResponse({"status": "uploaded"}, status=201)
