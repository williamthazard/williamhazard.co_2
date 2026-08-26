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
