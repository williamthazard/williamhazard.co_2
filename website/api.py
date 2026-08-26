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


import datetime as dt
import json as jsonlib

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

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


# --- DEBUG-only draft preview -------------------------------------------
#
# Deliberately reimplements the writer/draft.py header grammar (~20 lines
# below) rather than importing it: this is server code, and the writer
# package (with its own venv and deps) must never be a server dependency.


def drafts_dir():
    """Local drafts directory: env LOG_DRAFTS_DIR, else ~/Documents/log-drafts."""
    raw = os.environ.get("LOG_DRAFTS_DIR", "~/Documents/log-drafts")
    return os.path.expanduser(raw)


def _draft_path(slug):
    return os.path.join(drafts_dir(), f"{slug}.md")


class DraftHeaderError(Exception):
    def __init__(self, line, message):
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


def _parse_draft_header(text):
    """key: value lines until the first blank line, then body verbatim."""
    lines = text.split("\n")
    header = {}
    header_end_line = None
    body = ""
    for i, line in enumerate(lines):
        if line.strip() == "":
            body = "\n".join(lines[i + 1:])
            header_end_line = i + 1
            break
        if ":" not in line:
            raise DraftHeaderError(i + 1, f"expected 'key: value', got {line!r}")
        key, _, value = line.partition(":")
        header[key.strip()] = value.strip()
    else:
        raise DraftHeaderError(len(lines) or 1, "missing blank line separating header from body")
    if "title" not in header:
        raise DraftHeaderError(header_end_line, "missing required 'title' field")
    if "slug" not in header:
        raise DraftHeaderError(header_end_line, "missing required 'slug' field")
    return header, body


def _resolve_publish_date(date_str):
    if not date_str:
        return timezone.now()
    parsed = parse_datetime(date_str)
    if parsed is None:
        d = parse_date(date_str)
        if d is not None:
            parsed = dt.datetime.combine(d, dt.time.min)
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed) if settings.USE_TZ else parsed
    return parsed


def _inject_reload_script(html, slug):
    mtime_url = reverse("draft_preview_mtime", kwargs={"slug": slug})
    script = (
        "<script>"
        "(function(){"
        "var last=null;"
        f"function poll(){{fetch({mtime_url!r}).then(function(r){{return r.json();}})"
        ".then(function(d){"
        "if(last===null){last=d.mtime;}"
        "else if(d.mtime!==last){location.reload();}"
        "});}"
        "setInterval(poll,1500);"
        "})();"
        "</script></body>"
    )
    if "</body>" in html:
        return html.replace("</body>", script, 1)
    return html + script


def draft_preview(request, slug):
    if not settings.DEBUG:
        raise Http404("draft preview is only available in DEBUG")
    path = _draft_path(slug)
    if not os.path.isfile(path):
        raise Http404("no draft found for this slug")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        header, body = _parse_draft_header(text)
    except DraftHeaderError as e:
        return HttpResponse(f"draft parse error: {e}", content_type="text/plain")
    entry = LogEntry(
        pk=0,
        title=header["title"],
        slug=header["slug"],
        content_markdown=body,
        publish_date=_resolve_publish_date(header.get("date")),
    )
    html = render_to_string("log_detail.html", {"entry": entry}, request=request)
    return HttpResponse(_inject_reload_script(html, slug))


def draft_preview_mtime(request, slug):
    if not settings.DEBUG:
        raise Http404("draft preview is only available in DEBUG")
    path = _draft_path(slug)
    if not os.path.isfile(path):
        raise Http404("no draft found for this slug")
    return JsonResponse({"mtime": os.path.getmtime(path)})
