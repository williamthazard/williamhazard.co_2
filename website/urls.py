from django.urls import path
from . import views, api
from .feeds import LatestLogFeed

urlpatterns = [
    path('', views.home_view, name='home'),
    path('log/', views.log_index, name='log_index'),
    path('log/rss.xml', LatestLogFeed(), name='rss_feed'),
    path('log/<slug:entry_slug>/comment/', views.post_comment_view, name='post_comment'),
    path('log/<slug:entry_slug>/', views.log_detail, name='log_detail'),
    path('sketches/<path:path>', views.serve_sketches, name='serve_sketches'),
    path('media/<path:path>', views.serve_media, name='serve_media'),
    path('webmention/webhook/', views.webmention_webhook, name='webmention_webhook'),
    path('webmention/sync/', views.sync_webmentions_view, name='sync_webmentions'),
    path('wrapped/2025/', views.wrapped_2025_view, name='wrapped_2025'),
    path('wrapped/', views.wrapped_index_view, name='wrapped_index'),
    path('wrapped-2025/', views.wrapped_2025_redirect, name='wrapped_2025_redirect'),
    path('api/writer/ping', api.ping, name='api_writer_ping'),
    path('api/writer/entries', api.entries, name='api_writer_entries'),
    path('api/writer/entries/<slug:slug>/assets', api.entry_assets, name='api_writer_entry_assets'),
    path('api/writer/entries/<slug:slug>', api.entry, name='api_writer_entry'),
    path('api/writer/assets/<slug:slug>/<str:name>', api.asset_download, name='api_writer_asset_download'),
    path('api/writer/assets', api.assets, name='api_writer_assets'),
    path('draft-preview/<slug:slug>/mtime', api.draft_preview_mtime, name='draft_preview_mtime'),
    path('draft-preview/<slug:slug>/', api.draft_preview, name='draft_preview'),
    path('<slug:page_slug>/', views.page_view, name='page_detail'),
]
