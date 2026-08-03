import os
import requests
import re
import threading
from datetime import datetime, timezone
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import LogEntry

def post_to_bluesky(entry):
    handle = os.environ.get('BLUESKY_HANDLE')
    app_password = os.environ.get('BLUESKY_PASSWORD')
    pds_url = os.environ.get('BLUESKY_PDS_URL', 'https://bsky.social')
    
    if not handle or not app_password:
        print("Bluesky credentials missing from .env")
        return
        
    # 1. Create session
    session_endpoint = f'{pds_url}/xrpc/com.atproto.server.createSession'
    session_res = requests.post(session_endpoint, json={'identifier': handle, 'password': app_password}, timeout=15)
    session_res.raise_for_status()
    session = session_res.json()
    
    # 2. Upload first asset image if exists
    embed_images = []
    first_asset = entry.assets.first()
    if first_asset and first_asset.file:
        file_path = first_asset.file.path
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png']:
            upload_endpoint = f'{pds_url}/xrpc/com.atproto.repo.uploadBlob'
            headers = {'Content-Type': 'image/jpeg', 'Authorization': f"Bearer {session['accessJwt']}"}
            with open(file_path, 'rb') as f:
                blob_res = requests.post(upload_endpoint, headers=headers, data=f.read(), timeout=15)
                if blob_res.status_code == 200:
                    embed_images.append({
                        'image': blob_res.json()['blob'],
                        'alt': "Attached image for log entry"
                    })
                    
    # 3. Resolve mentions in text
    text = entry.content_markdown
    # Strip markdown links and format clean text
    text_clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    if len(text_clean) > 280:
        text_clean = text_clean[:277] + "..."
        
    post_data = {
        '$type': 'app.bsky.feed.post',
        'text': text_clean,
        'createdAt': datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    
    if embed_images:
        post_data['embed'] = {
            '$type': 'app.bsky.embed.images',
            'images': embed_images
        }
        
    record_endpoint = f'{pds_url}/xrpc/com.atproto.repo.createRecord'
    record_data = {
        'repo': session['did'],
        'collection': 'app.bsky.feed.post',
        'record': post_data
    }
    headers = {'Authorization': f"Bearer {session['accessJwt']}"}
    requests.post(record_endpoint, headers=headers, json=record_data, timeout=15).raise_for_status()


def post_to_mastodon(entry):
    token = os.environ.get('MASTODON_ACCESS_TOKEN')
    base_url = os.environ.get('MASTODON_API_BASE_URL')
    
    if not token or not base_url:
        print("Mastodon credentials missing from .env")
        return
        
    # 1. Upload first media file if exists
    media_ids = []
    first_asset = entry.assets.first()
    if first_asset and first_asset.file:
        file_path = first_asset.file.path
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4']:
            upload_endpoint = f'{base_url}/api/v1/media'
            headers = {'Authorization': f"Bearer {token}"}
            with open(file_path, 'rb') as f:
                files = {'file': (os.path.basename(file_path), f)}
                media_res = requests.post(upload_endpoint, headers=headers, files=files, timeout=15)
                if media_res.status_code == 200:
                    media_ids.append(media_res.json()['id'])
                    
    # 2. Post status
    status_endpoint = f'{base_url}/api/v1/statuses'
    headers = {'Authorization': f"Bearer {token}"}
    
    text_clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', entry.content_markdown)
    # Append website entry link if slug exists
    post_link = f"\n\nRead more: https://williamhazard.co/log/{entry.slug}/"
    
    if len(text_clean) + len(post_link) > 500:
        text_clean = text_clean[:500 - len(post_link) - 3] + "..."
    status_text = text_clean + post_link
    
    payload = {'status': status_text}
    if media_ids:
        payload['media_ids[]'] = media_ids
        
    requests.post(status_endpoint, headers=headers, data=payload, timeout=15).raise_for_status()


@receiver(post_save, sender=LogEntry)
def handle_social_cross_posting(sender, instance, created, **kwargs):
    need_bluesky = instance.share_to_bluesky and not instance.posted_to_bluesky
    need_mastodon = instance.share_to_mastodon and not instance.posted_to_mastodon
    
    if not need_bluesky and not need_mastodon:
        return

    def run_posting():
        updated_fields = []
        
        if need_bluesky:
            try:
                post_to_bluesky(instance)
                instance.posted_to_bluesky = True
                updated_fields.append('posted_to_bluesky')
            except Exception as e:
                print(f"Failed to post to Bluesky: {e}")
                
        if need_mastodon:
            try:
                post_to_mastodon(instance)
                instance.posted_to_mastodon = True
                updated_fields.append('posted_to_mastodon')
            except Exception as e:
                print(f"Failed to post to Mastodon: {e}")
                
        if updated_fields:
            LogEntry.objects.filter(pk=instance.pk).update(**{f: getattr(instance, f) for f in updated_fields})

    transaction.on_commit(lambda: threading.Thread(target=run_posting).start())
