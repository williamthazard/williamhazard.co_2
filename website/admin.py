from django.contrib import admin
from django.utils.safestring import mark_safe
from .models import Page, LogEntry, LogAsset, PageAsset

class PageAssetInline(admin.TabularInline):
    model = PageAsset
    extra = 1
    fields = ('file', 'custom_filename', 'copyable_snippet')
    readonly_fields = ('copyable_snippet',)

    def copyable_snippet(self, instance):
        if instance.file:
            url = instance.file.url
            return mark_safe(
                f'<code style="background:#eee; padding:2px 5px;">{url}</code>'
            )
        return "Save model to see snippet"
    copyable_snippet.short_description = "Media URL Snippet"

class LogAssetInline(admin.TabularInline):
    model = LogAsset
    extra = 1
    fields = ('file', 'custom_filename', 'copyable_snippet')
    readonly_fields = ('copyable_snippet',)

    def copyable_snippet(self, instance):
        if instance.file:
            url = instance.file.url
            # Display helpful, copyable code tags for images/videos in admin
            return mark_safe(
                f'<code style="background:#eee; padding:2px 5px;">{url}</code>'
            )
        return "Save model to see snippet"
    copyable_snippet.short_description = "Media URL Snippet"

@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'publish_date', 'posted_to_bluesky', 'posted_to_mastodon')
    search_fields = ('title', 'content_markdown')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [LogAssetInline]
    
    # Organise social sharing fields
    fieldsets = (
        (None, {
            'fields': ('title', 'slug', 'content_markdown', 'publish_date')
        }),
        ('Social Sharing', {
            'fields': ('share_to_bluesky', 'share_to_mastodon', 'posted_to_bluesky', 'posted_to_mastodon'),
            'description': 'Configure automatic publishing to networks on save.'
        }),
    )
    readonly_fields = ('posted_to_bluesky', 'posted_to_mastodon')

@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ('title', 'slug', 'updated_at')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [PageAssetInline]

@admin.register(PageAsset)
class PageAssetAdmin(admin.ModelAdmin):
    list_display = ('id', 'page', 'file', 'custom_filename')
    list_filter = ('page',)
    search_fields = ('custom_filename', 'file')

@admin.register(LogAsset)
class LogAssetAdmin(admin.ModelAdmin):
    list_display = ('id', 'log_entry', 'file', 'custom_filename')
    list_filter = ('log_entry',)
    search_fields = ('custom_filename', 'file')

