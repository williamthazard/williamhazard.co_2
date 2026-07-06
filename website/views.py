import os
import mimetypes
from django.shortcuts import render, get_object_or_404, redirect
from django.http import Http404, FileResponse
from django.conf import settings
from django.core.paginator import Paginator
from .models import Page, LogEntry

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

def serve_gbg(request, path):
    return serve_static_project(request, 'gbg', path)

def serve_media(request, path):
    return serve_static_project(request, 'media', path)
