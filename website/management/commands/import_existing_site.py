import os
import re
import shutil
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from website.models import Page, LogEntry, LogAsset

class Command(BaseCommand):
    help = "Imports all content and assets from the legacy williamhazard.co static website."

    def add_arguments(self, parser):
        parser.add_argument('--repo-path', type=str, required=True, help="Absolute path to williamhazard.co repository root")

    def handle(self, *args, **options):
        repo_path = options['repo_path']
        if not os.path.exists(repo_path):
            self.stdout.write(self.style.ERROR(f"Repository path does not exist: {repo_path}"))
            return

        # Ensure target media directories exist
        page_media_dir = os.path.join(settings.MEDIA_ROOT, 'page_assets')
        log_media_dir = os.path.join(settings.MEDIA_ROOT, 'log_assets')
        os.makedirs(page_media_dir, exist_ok=True)
        os.makedirs(log_media_dir, exist_ok=True)

        # 1. Import Page models (static sections)
        pages_to_import = [
            ('', 'home'), # root index.md
            ('words', 'words'),
            ('sounds', 'sounds'),
            ('code', 'code'),
            ('videos', 'videos'),
            ('events', 'events'),
            ('bio', 'bio'),
            ('sketches', 'sketches'),
            ('gbg', 'gbg')
        ]

        self.stdout.write(">> Importing main pages...")
        for subdir, slug in pages_to_import:
            file_path = os.path.join(repo_path, subdir, 'index.md')
            if not os.path.exists(file_path):
                self.stdout.write(self.style.WARNING(f"File not found: {file_path}"))
                continue
                
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Copy page-related images to page_assets
            images = re.findall(r'!\[.*?\]\(([^)]+)\)', content)
            for img in images:
                if not img.startswith(('http', '/')):
                    src_img_path = os.path.normpath(os.path.join(repo_path, subdir, img))
                    if os.path.exists(src_img_path):
                        dest_img_name = os.path.basename(src_img_path)
                        dest_img_path = os.path.join(page_media_dir, dest_img_name)
                        shutil.copy2(src_img_path, dest_img_path)
                        
                        # Rewrite path inside markdown
                        content = content.replace(img, f"/media/page_assets/{dest_img_name}")

            Page.objects.update_or_create(
                slug=slug,
                defaults={
                    'title': slug.capitalize() if slug != 'home' else 'William Hazard',
                    'content_markdown': content
                }
            )
            self.stdout.write(self.style.SUCCESS(f"Imported page: {slug}"))

        # 2. Import Log entries (blog posts)
        self.stdout.write("\n>> Importing log entries...")
        log_entries_dir = os.path.join(repo_path, 'log', 'entries')
        if os.path.exists(log_entries_dir):
            for filename in os.listdir(log_entries_dir):
                if not filename.endswith('.md'):
                    continue
                    
                file_path = os.path.join(log_entries_dir, filename)
                slug = os.path.splitext(filename)[0]
                
                # Parse date prefix: e.g. 230919-bear -> 2023-09-19
                date_match = re.match(r'^(\d{2})(\d{2})(\d{2})', slug)
                if date_match:
                    try:
                        pub_date = datetime.strptime(slug[:6], "%y%m%d")
                        pub_date = timezone.make_aware(pub_date, timezone.UTC)
                    except ValueError:
                        mtime = os.path.getmtime(file_path)
                        pub_date = timezone.make_aware(datetime.fromtimestamp(mtime), timezone.UTC)
                    title = slug.split('-', 1)[1] if '-' in slug else slug
                else:
                    mtime = os.path.getmtime(file_path)
                    pub_date = timezone.make_aware(datetime.fromtimestamp(mtime), timezone.UTC)
                    title = slug

                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Create log entry record first
                # Mark as already posted to prevent social API triggers during migration
                log_entry, created = LogEntry.objects.update_or_create(
                    slug=slug,
                    defaults={
                        'title': title,
                        'content_markdown': content,
                        'publish_date': pub_date,
                        'posted_to_bluesky': True,
                        'posted_to_mastodon': True
                    }
                )

                # 1. Replace HTML attributes (src="pics/...", href="audio/...", poster="pics/...")
                html_pattern = r'(?P<prefix>(?:src|href|poster)=")(?P<path>[^"]+)(?P<suffix>")'
                
                def replace_html_asset(match):
                    prefix = match.group('prefix')
                    path = match.group('path')
                    suffix = match.group('suffix')
                    
                    if path.startswith(('pics/', 'audio/', 'pics/audio/')):
                        asset_name = os.path.basename(path)
                        
                        paths_to_check = [
                            os.path.join(repo_path, 'log', 'entries', 'pics', asset_name),
                            os.path.join(repo_path, 'log', 'pics', asset_name),
                            os.path.join(repo_path, 'log', 'audio', asset_name),
                            os.path.join(repo_path, 'log', 'entries', 'audio', asset_name),
                        ]
                        
                        src_asset_path = None
                        for path_cand in paths_to_check:
                            if os.path.exists(path_cand):
                                src_asset_path = path_cand
                                break
                                
                        if src_asset_path:
                            dest_name = asset_name
                            dest_path = os.path.join(log_media_dir, dest_name)
                            shutil.copy2(src_asset_path, dest_path)
                            
                            # Register asset record in database
                            LogAsset.objects.get_or_create(
                                log_entry=log_entry,
                                file=f"log_assets/{dest_name}",
                                defaults={'custom_filename': dest_name}
                            )
                            return f"{prefix}/media/log_assets/{dest_name}{suffix}"
                    return match.group(0)

                content = re.sub(html_pattern, replace_html_asset, content)

                # 2. Replace Markdown links/images (![alt](path 'title') or [text](path))
                md_pattern = r'(?P<prefix>!?\[.*?\]\()(?P<url>[^)]+)(?P<suffix>\))'
                
                def replace_md_asset(match):
                    prefix = match.group('prefix')
                    url_part = match.group('url').strip()
                    suffix = match.group('suffix')
                    
                    # Ignore absolute URLs or root paths
                    if url_part.startswith(('http', '/', '#')):
                        return match.group(0)
                        
                    parts = url_part.split(None, 1)
                    path = parts[0]
                    title_part = " " + parts[1] if len(parts) > 1 else ""
                    
                    # Extract the filename from the path
                    asset_name = os.path.basename(path)
                    
                    paths_to_check = [
                        os.path.join(repo_path, 'log', 'entries', 'pics', asset_name),
                        os.path.join(repo_path, 'log', 'pics', asset_name),
                        os.path.join(repo_path, 'log', 'audio', asset_name),
                        os.path.join(repo_path, 'log', 'entries', 'audio', asset_name),
                    ]
                    
                    src_asset_path = None
                    for path_cand in paths_to_check:
                        if os.path.exists(path_cand):
                            src_asset_path = path_cand
                            break
                            
                    if src_asset_path:
                        dest_name = asset_name
                        dest_path = os.path.join(log_media_dir, dest_name)
                        shutil.copy2(src_asset_path, dest_path)
                        
                        # Register asset record in database
                        LogAsset.objects.get_or_create(
                            log_entry=log_entry,
                            file=f"log_assets/{dest_name}",
                            defaults={'custom_filename': dest_name}
                        )
                        return f"{prefix}/media/log_assets/{dest_name}{title_part}{suffix}"
                        
                    return match.group(0)

                content = re.sub(md_pattern, replace_md_asset, content)

                # Save final corrected content
                log_entry.content_markdown = content
                log_entry.save()
                self.stdout.write(self.style.SUCCESS(f"Imported log entry: {slug}"))

        # 3. Copy Sketches and GBG folders intact
        self.stdout.write("\n>> Copying sketches and gbg static folders...")
        for folder in ['sketches', 'gbg']:
            src_folder = os.path.join(repo_path, folder)
            dest_folder = os.path.join(settings.BASE_DIR, folder)
            
            if os.path.exists(src_folder):
                if os.path.exists(dest_folder):
                    shutil.rmtree(dest_folder)
                
                # Custom ignore callback to only ignore index.html at top-level
                def ignore_top_level_indexes(directory, contents):
                    is_root = os.path.basename(directory) == folder
                    ignored = ['index.md']
                    if is_root and 'index.html' in contents:
                        ignored.append('index.html')
                    return ignored

                # Copy directories, ignoring only root index.md and root index.html
                shutil.copytree(
                    src_folder, 
                    dest_folder, 
                    symlinks=True, 
                    ignore=ignore_top_level_indexes
                )
                self.stdout.write(self.style.SUCCESS(f"Copied {folder} folder structure successfully."))
                
        self.stdout.write(self.style.SUCCESS("\n>> Import complete!"))
