import os
import subprocess
import threading
from django.db import models

class Page(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, help_text="Used in the URL (e.g. 'words', 'sounds').")
    content_markdown = models.TextField(help_text="Raw Markdown content.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


class PageAsset(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name='assets')
    file = models.FileField(upload_to='page_assets/')
    custom_filename = models.CharField(
        max_length=100, 
        blank=True, 
        help_text="Optional: Rename file (e.g. 'performance.jpg'). Leave blank to auto-rename based on page slug."
    )

    def __str__(self):
        return os.path.basename(self.file.name) if self.file else f"PageAsset #{self.id}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.file:
            old_path = self.file.path
            ext = os.path.splitext(self.file.name)[1].lower()
            
            if self.custom_filename:
                from django.utils.text import get_valid_filename
                cleaned_name = get_valid_filename(os.path.basename(self.custom_filename))
                new_name = cleaned_name
                if not new_name.endswith(ext):
                    new_name = os.path.splitext(new_name)[0] + ext
            else:
                import uuid
                suffix = uuid.uuid4().hex[:8]
                new_name = f"{self.page.slug}-{suffix}{ext}"
            
            new_relative_path = os.path.join('page_assets', new_name)
            new_absolute_path = os.path.join(os.path.dirname(old_path), new_name)
            
            if old_path != new_absolute_path:
                if old_path.lower() == new_absolute_path.lower():
                    temp_path = old_path + '.tmp_rename'
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    os.rename(old_path, temp_path)
                    os.rename(temp_path, new_absolute_path)
                else:
                    if os.path.exists(new_absolute_path):
                        os.remove(new_absolute_path)
                    os.rename(old_path, new_absolute_path)
                self.file.name = new_relative_path
                super().save(update_fields=['file'])
                
            # Trigger background processing for image resizing
            threading.Thread(target=self.compress_asset, args=(new_absolute_path, ext)).start()

    def compress_asset(self, file_path, ext):
        if ext in ['.jpg', '.jpeg']:
            try:
                subprocess.run([
                    'mogrify', '-resize', '800x450^', '-gravity', 'center',
                    '-extent', '800x450', '-strip', file_path
                ], check=True)
            except Exception as e:
                print(f"Error compressing JPEG asset: {e}")


class LogEntry(models.Model):
    title = models.CharField(max_length=200, help_text="E.g., 'bear' or '240809'")
    slug = models.SlugField(unique=True, help_text="E.g., '230919-bear' or '240809'")
    content_markdown = models.TextField(help_text="Raw Markdown content.")
    publish_date = models.DateTimeField(help_text="Publish date of the entry.")
    
    share_to_bluesky = models.BooleanField(default=False, help_text="Post this entry to Bluesky upon saving.")
    share_to_mastodon = models.BooleanField(default=False, help_text="Post this entry to Mastodon upon saving.")
    
    posted_to_bluesky = models.BooleanField(default=False)
    posted_to_mastodon = models.BooleanField(default=False)

    class Meta:
        ordering = ['-publish_date']
        verbose_name_plural = "Log Entries"

    def __str__(self):
        return self.title


class LogAsset(models.Model):
    log_entry = models.ForeignKey(LogEntry, on_delete=models.CASCADE, related_name='assets')
    file = models.FileField(upload_to='log_assets/')
    custom_filename = models.CharField(
        max_length=100, 
        blank=True, 
        help_text="Optional: Rename file (e.g. 'glow.mp4'). Leave blank to auto-rename based on log slug."
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        
        if self.file:
            old_path = self.file.path
            ext = os.path.splitext(self.file.name)[1].lower()
            
            if self.custom_filename:
                from django.utils.text import get_valid_filename
                cleaned_name = get_valid_filename(os.path.basename(self.custom_filename))
                new_name = cleaned_name
                if not new_name.endswith(ext):
                    new_name = os.path.splitext(new_name)[0] + ext
            else:
                import uuid
                suffix = uuid.uuid4().hex[:8]
                new_name = f"{self.log_entry.slug}-{suffix}{ext}"
            
            new_relative_path = os.path.join('log_assets', new_name)
            new_absolute_path = os.path.join(os.path.dirname(old_path), new_name)
            
            if old_path != new_absolute_path:
                if old_path.lower() == new_absolute_path.lower():
                    temp_path = old_path + '.tmp_rename'
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    os.rename(old_path, temp_path)
                    os.rename(temp_path, new_absolute_path)
                else:
                    if os.path.exists(new_absolute_path):
                        os.remove(new_absolute_path)
                    os.rename(old_path, new_absolute_path)
                self.file.name = new_relative_path
                super().save(update_fields=['file'])
                
            # Trigger background processing
            threading.Thread(target=self.compress_asset, args=(new_absolute_path, ext)).start()

    def compress_asset(self, file_path, ext):
        if ext in ['.jpg', '.jpeg']:
            try:
                subprocess.run([
                    'mogrify', '-resize', '800x450^', '-gravity', 'center',
                    '-extent', '800x450', '-strip', file_path
                ], check=True)
            except Exception as e:
                print(f"Error compressing JPEG asset: {e}")
                
        elif ext in ['.mov', '.mp4']:
            base_path, _ = os.path.splitext(file_path)
            mp4_path = f"{base_path}.mp4"
            ogg_path = f"{base_path}.ogg"
            webm_path = f"{base_path}.webm"
            poster_path = f"{base_path}.jpeg"
            
            # 1. MP4 conversion
            if file_path.endswith('.mov') or not os.path.exists(mp4_path):
                try:
                    try:
                        subprocess.run([
                            'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                            '-c:v', 'h264_videotoolbox', '-q:v', '50', '-movflags', '+faststart', mp4_path
                        ], check=True)
                    except subprocess.CalledProcessError:
                        subprocess.run([
                            'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                            '-c:v', 'libx264', '-crf', '28', '-movflags', '+faststart', mp4_path
                        ], check=True)
                except Exception as e:
                    print(f"Error converting video to MP4: {e}")
            
            # 2. OGG conversion
            if not os.path.exists(ogg_path):
                try:
                    subprocess.run([
                        'ffmpeg', '-y', '-i', file_path, ogg_path
                    ], check=True)
                except Exception as e:
                    print(f"Error converting video to OGG: {e}")
                    
            # 3. WEBM conversion
            if not os.path.exists(webm_path):
                try:
                    subprocess.run([
                        'ffmpeg', '-y', '-i', file_path, '-q:v', '50', webm_path
                    ], check=True)
                except Exception as e:
                    print(f"Error converting video to WEBM: {e}")
            
            # 4. Poster extraction
            if not os.path.exists(poster_path):
                try:
                    src_for_poster = mp4_path if os.path.exists(mp4_path) else file_path
                    subprocess.run([
                        'ffmpeg', '-y', '-i', src_for_poster, '-frames:v', '1', '-f', 'image2', poster_path
                    ], check=True)
                except Exception as e:
                    print(f"Error extracting poster frame: {e}")
                
            # 5. Large MOV deletion/recompression
            if ext == '.mov' and os.path.exists(file_path):
                try:
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    if size_mb >= 100:
                        os.remove(file_path)
                        try:
                            subprocess.run([
                                'ffmpeg', '-y', '-i', mp4_path, '-preset', 'veryfast',
                                '-c:v', 'h264_videotoolbox', '-q:v', '50', '-movflags', '+faststart', file_path
                            ], check=True)
                        except subprocess.CalledProcessError:
                            subprocess.run([
                                'ffmpeg', '-y', '-i', mp4_path, '-preset', 'veryfast',
                                '-c:v', 'libx264', '-crf', '28', '-movflags', '+faststart', file_path
                            ], check=True)
                except Exception as e:
                    print(f"Error compressing original large MOV file: {e}")


class Webmention(models.Model):
    log_entry = models.ForeignKey(
        LogEntry, 
        on_delete=models.SET_NULL, 
        related_name='webmentions', 
        null=True, 
        blank=True,
        help_text="Associated log entry post, if matched by target URL."
    )
    target_url = models.URLField(max_length=500, help_text="Target URL on this site.")
    source_url = models.URLField(max_length=500, help_text="Source URL linking to target.")
    wm_id = models.IntegerField(unique=True, null=True, blank=True, help_text="Webmention.io internal ID.")
    comment_type = models.CharField(max_length=50, default='reply', help_text="Type: reply, like, repost, mention.")
    author_name = models.CharField(max_length=200, blank=True, help_text="Author display name.")
    author_photo = models.URLField(max_length=500, blank=True, help_text="Author avatar image URL.")
    author_url = models.URLField(max_length=500, blank=True, help_text="Author homepage/profile URL.")
    content_html = models.TextField(blank=True, help_text="Rendered HTML content of the mention.")
    content_text = models.TextField(blank=True, help_text="Plain text content of the mention.")
    published_at = models.DateTimeField(null=True, blank=True, help_text="Publication date of the mention.")
    received_at = models.DateTimeField(auto_now_add=True, help_text="Timestamp when received by our webhook.")
    is_approved = models.BooleanField(default=True, help_text="Approval flag for displaying on the site.")

    class Meta:
        ordering = ['-published_at', '-received_at']
        verbose_name = "Webmention"
        verbose_name_plural = "Webmentions"

    def __str__(self):
        author = self.author_name or self.source_url
        return f"{self.comment_type} from {author} on {self.target_url}"
