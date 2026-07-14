from django.urls import path
from . import views
from .feeds import LatestLogFeed

urlpatterns = [
    path('', views.home_view, name='home'),
    path('log/', views.log_index, name='log_index'),
    path('log/rss.xml', LatestLogFeed(), name='rss_feed'),
    path('log/<slug:entry_slug>/', views.log_detail, name='log_detail'),
    path('sketches/<path:path>', views.serve_sketches, name='serve_sketches'),
    path('media/<path:path>', views.serve_media, name='serve_media'),
    path('<slug:page_slug>/', views.page_view, name='page_detail'),
]
