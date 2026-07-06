from django.urls import path
from . import views
from .feeds import LatestLogFeed

urlpatterns = [
    path('', views.home_view, name='home'),
    path('log/', views.log_index, name='log_index'),
    path('log/rss.xml', LatestLogFeed(), name='rss_feed'),
    path('log/<slug:entry_slug>/', views.log_detail, name='log_detail'),
    path('sketches/<path:path>', views.serve_sketches, name='serve_sketches'),
    path('gbg/<path:path>', views.serve_gbg, name='serve_gbg'),
    path('<slug:page_slug>/', views.page_view, name='page_detail'),
]
