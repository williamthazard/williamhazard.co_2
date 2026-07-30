import os
import json
import mimetypes
from urllib.parse import urlparse
from django.shortcuts import render, get_object_or_404, redirect
from django.http import Http404, FileResponse, JsonResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.conf import settings
from django.core.paginator import Paginator
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from .models import Page, LogEntry, Webmention

def home_view(request):
    # Serve page with slug 'home' as homepage
    page = get_object_or_404(Page, slug='home')
    return render(request, 'page_detail.html', {'page': page})

def page_view(request, page_slug):
    page = get_object_or_404(Page, slug=page_slug)
    return render(request, 'page_detail.html', {'page': page})

def log_index(request):
    entry_list = LogEntry.objects.all()
    paginator = Paginator(entry_list, 10) # 10 entries per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'log_index.html', {'page_obj': page_obj})

def log_detail(request, entry_slug):
    entry = get_object_or_404(LogEntry, slug=entry_slug)
    return render(request, 'log_detail.html', {'entry': entry})

def serve_static_project(request, folder, path):
    base_dir = os.path.realpath(os.path.join(settings.BASE_DIR, folder))
    file_path = os.path.realpath(os.path.join(base_dir, path))
    
    # Security check to prevent directory traversal
    prefix = base_dir if base_dir.endswith(os.sep) else base_dir + os.sep
    if not (file_path == base_dir or file_path.startswith(prefix)):
        raise Http404("Access denied")
        
    if os.path.isdir(file_path):
        if not request.path.endswith('/'):
            return redirect(request.path + '/')
        file_path = os.path.join(file_path, 'index.html')
        
    if not os.path.exists(file_path):
        raise Http404("Not found")
        
    content_type, encoding = mimetypes.guess_type(file_path)
    content_type = content_type or 'application/octet-stream'
    
    return FileResponse(open(file_path, 'rb'), content_type=content_type)


def serve_sketches(request, path):
    return serve_static_project(request, 'sketches', path)

def serve_media(request, path):
    return serve_static_project(request, 'media', path)


@csrf_exempt
def webmention_webhook(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST method allowed")

    expected_secret = os.environ.get('WEBMENTION_IO_SECRET')
    if expected_secret:
        incoming_secret = request.GET.get('secret') or request.headers.get('X-Webmention-Secret')
        if not incoming_secret:
            try:
                body_data = json.loads(request.body.decode('utf-8'))
                incoming_secret = body_data.get('secret')
            except Exception:
                pass
        if incoming_secret != expected_secret:
            return HttpResponseForbidden("Invalid secret token")

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception as e:
        return HttpResponseBadRequest(f"Invalid JSON payload: {e}")

    source = payload.get('source')
    target = payload.get('target')
    post_data = payload.get('post', {})

    if not source or not target:
        return HttpResponseBadRequest("Missing 'source' or 'target'")

    wm_id = post_data.get('wm-id')
    wm_property = post_data.get('wm-property', 'reply')
    
    property_map = {
        'in-reply-to': 'reply',
        'like-of': 'like',
        'repost-of': 'repost',
        'mention-of': 'mention',
    }
    comment_type = property_map.get(wm_property, wm_property or 'reply')

    author_data = post_data.get('author', {})
    author_name = author_data.get('name', '')
    author_photo = author_data.get('photo', '')
    author_url = author_data.get('url', '')

    content_data = post_data.get('content', {})
    content_html = content_data.get('html', '')
    content_text = content_data.get('text', '')

    published_str = post_data.get('published')
    published_at = parse_datetime(published_str) if published_str else None

    parsed_target = urlparse(target)
    path_parts = [p for p in parsed_target.path.split('/') if p]
    
    log_entry = None
    if len(path_parts) >= 2 and path_parts[0] == 'log':
        slug = path_parts[1]
        log_entry = LogEntry.objects.filter(slug=slug).first()

    lookup_kwargs = {'wm_id': wm_id} if wm_id else {'source_url': source, 'target_url': target}
    defaults = {
        'target_url': target,
        'source_url': source,
        'comment_type': comment_type,
        'author_name': author_name,
        'author_photo': author_photo,
        'author_url': author_url,
        'content_html': content_html,
        'content_text': content_text,
        'published_at': published_at,
        'log_entry': log_entry,
    }

    wm_obj, created = Webmention.objects.update_or_create(**lookup_kwargs, defaults=defaults)

    return JsonResponse({
        'status': 'ok',
        'id': wm_obj.id,
        'created': created,
        'matched_log_entry': log_entry.slug if log_entry else None
    })
