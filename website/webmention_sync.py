import os
import requests
from urllib.parse import urlparse
from django.utils.dateparse import parse_datetime
from .models import Webmention, LogEntry

def sync_webmentions_from_api(token=None, domain=None):
    token = token or os.environ.get('WEBMENTION_IO_TOKEN')
    if not token:
        return {'status': 'error', 'message': 'WEBMENTION_IO_TOKEN not configured'}

    url = f"https://webmention.io/api/mentions.jf2?token={token}"
    if domain:
        url += f"&domain={domain}"

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

    children = data.get('children', [])
    created_count = 0
    updated_count = 0

    property_map = {
        'in-reply-to': 'reply',
        'like-of': 'like',
        'repost-of': 'repost',
        'mention-of': 'mention',
    }

    for item in children:
        source = item.get('wm-source') or item.get('url')
        target = item.get('wm-target')
        wm_id = item.get('wm-id')
        wm_property = item.get('wm-property', 'reply')
        comment_type = property_map.get(wm_property, wm_property or 'reply')

        if not source or not target:
            continue

        author_data = item.get('author', {})
        author_name = author_data.get('name', '')
        author_photo = author_data.get('photo', '')
        author_url = author_data.get('url', '')

        content_data = item.get('content', {})
        content_html = content_data.get('html', '')
        content_text = content_data.get('text', '')

        published_str = item.get('published') or item.get('wm-received')
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
        if created:
            created_count += 1
        else:
            updated_count += 1

    return {
        'status': 'ok',
        'total_fetched': len(children),
        'created': created_count,
        'updated': updated_count
    }
