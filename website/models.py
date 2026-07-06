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
                new_name = self.custom_filename
                if not new_name.endswith(ext):
                    new_name = os.path.splitext(new_name)[0] + ext
            else:
                counter = self.log_entry.assets.filter(id__lt=self.id).count() + 1
                new_name = f"{self.log_entry.slug}-{counter}{ext}"
            
            new_relative_path = os.path.join('log_assets', new_name)
            new_absolute_path = os.path.join(os.path.dirname(old_path), new_name)
            
            if old_path != new_absolute_path:
                if os.path.exists(new_absolute_path):
                    os.remove(new_absolute_path)
                os.rename(old_path, new_absolute_path)
                self.file.name = new_relative_path
                super().save(update_fields=['file'])
                
            # Trigger background processing
            threading.Thread(target=self.compress_asset, args=(new_absolute_path, ext)).start()

    def compress_asset(self, file_path, ext):
        try:
            if ext in ['.jpg', '.jpeg']:
                subprocess.run([
                    'mogrify', '-resize', '800x450^', '-gravity', 'center',
                    '-extent', '16:9', '-strip', file_path
                ], check=True)
                
            elif ext in ['.mov', '.mp4']:
                base_path, _ = os.path.splitext(file_path)
                mp4_path = f"{base_path}.mp4"
                if file_path.endswith('.mov') or not os.path.exists(mp4_path):
                    # Try macOS hardware accelerated encoding first
                    try:
                        subprocess.run([
                            'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                            '-c:v', 'h264_videotoolbox', '-q:v', '50', '-movflags', '+faststart', mp4_path
                        ], check=True)
                    except subprocess.CalledProcessError:
                        # Fallback to standard CPU/libx264 encoding (e.g. on Linux or if videotoolbox fails)
                        subprocess.run([
                            'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                            '-c:v', 'libx264', '-crf', '28', '-movflags', '+faststart', mp4_path
                        ], check=True)
                
                ogg_path = f"{base_path}.ogg"
                if not os.path.exists(ogg_path):
                    subprocess.run([
                        'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                        '-movflags', '+faststart', ogg_path
                    ], check=True)
                    
                webm_path = f"{base_path}.webm"
                if not os.path.exists(webm_path):
                    subprocess.run([
                        'ffmpeg', '-y', '-i', file_path, '-preset', 'veryfast',
                        '-q:v', '50', '-movflags', '+faststart', webm_path
                    ], check=True)
                
                poster_path = f"{base_path}.jpeg"
                if not os.path.exists(poster_path):
                    subprocess.run([
                        'ffmpeg', '-i', mp4_path, '-frames:v', '1', '-f', 'image2', poster_path
                    ], check=True)
                    
                if ext == '.mov' and os.path.exists(file_path):
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
            print(f"Error compressing asset: {e}")
