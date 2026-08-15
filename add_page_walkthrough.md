# Walkthrough: Adding a New Page (MVT / MVC in Action)

This walkthrough guides you through adding a new page (e.g., a new "Colophon" page at `/colophon/`) to this website. It demonstrates how Django's MVC/MVT flow resolves the request dynamically, and details the exact commands to update both your local database and the live production site on Render.

---

## The MVC Flow of a New Page

In this project, the **Controller** layer is already designed dynamically. You do **not** need to write new Python views or edit URL routing code to add a page. Instead:

```
[Request /colophon/] 
       │
       ▼
1. CONTROLLER (urls.py + page_view in views.py)
   Reads the URL slug "colophon" and queries the database.
       │
       ▼
2. MODEL (models.py: Page object)
   Finds the database record where slug = "colophon" and returns it.
       │
       ▼
3. VIEW (templates/page_detail.html)
   Renders the markdown content of the returned Page object to HTML.
```

Adding a new page simply requires adding the **Model data** to the database.

---

## Step-by-Step Implementation

### Step 1: Define the Page Data (Model)
To ensure the new page is added reliably to both your local environment and your production database on Render, you should define it in a **Django Data Migration**.

1. Create a new empty migration file by running:
   ```bash
   ./.venv/bin/python manage.py makemigrations --empty website --name add_colophon_page
   ```
2. Open the newly created migration file (e.g., `website/migrations/0006_add_colophon_page.py`) and populate it with a migration that inserts the page into the database:
   ```python
   from django.db import migrations

   def create_colophon_page(apps, schema_editor):
       Page = apps.get_model('website', 'Page')
       Page.objects.get_or_create(
           slug='colophon',
           defaults={
               'title': 'Colophon',
               'content_markdown': '# Colophon\n\nThis site is made with computers.'
           }
       )

   def delete_colophon_page(apps, schema_editor):
       Page = apps.get_model('website', 'Page')
       Page.objects.filter(slug='colophon').delete()

   class Migration(migrations.Migration):

       dependencies = [
           # Link to your previous migration (e.g. 0005_delete_gbg_page)
           ('website', '0005_delete_gbg_page'),
       ]

       operations = [
           migrations.RunPython(create_colophon_page, reverse_code=delete_colophon_page),
       ]
   ```

*(Optionally, you could also add it directly to [website_fixture.json](file:///Users/spencergraham/Desktop/other/website-redux/william_hazard_project/website/fixtures/website_fixture.json), but migration files are preferred for database sync safety).*

---

### Step 2: Update Your Local Database
Run the migration command locally to push the new page data to your local SQLite database:
```bash
./.venv/bin/python manage.py migrate
```

Once run, you can start the local development server:
```bash
./.venv/bin/python manage.py runserver
```
And navigate to `http://127.0.0.1:8000/colophon/` in your browser. The page will render automatically!

---

### Step 3: Understand the routing (Controller)
When you visited `/colophon/`, the request flowed through the Controller:
1. [urls.py](file:///Users/spencergraham/Desktop/other/website-redux/william_hazard_project/website/urls.py#L13) intercepted the request using the dynamic slug pattern:
   ```python
   path('<slug:page_slug>/', views.page_view, name='page_detail'),
   ```
2. [views.py](file:///Users/spencergraham/Desktop/other/website-redux/william_hazard_project/website/views.py#L14-L16) executed `page_view`, querying the database for a `Page` model with `slug='colophon'`:
   ```python
   def page_view(request, page_slug):
       page = get_object_or_404(Page, slug=page_slug)
       return render(request, 'page_detail.html', {'page': page})
   ```

---

### Step 4: Render to the UI (View/Template)
The view template [page_detail.html](file:///Users/spencergraham/Desktop/other/website-redux/william_hazard_project/templates/page_detail.html) dynamically formats and displays the text:
```html
{% block content %}
  <main>
    <h1>{{ page.title }}</h1>
    <div class="markdown-body">
      {{ page.content_markdown }}
    </div>
  </main>
{% endblock %}
```

---

### Step 5: Commit and Deploy to the Live Site
To deploy this page to your live site, commit the migration file and push to GitHub. 

1. **Stage and commit the changes:**
   ```bash
   git add website/migrations/0006_add_colophon_page.py
   git commit -m "feat: add colophon page data migration"
   ```
2. **Push to Github:**
   ```bash
   git push origin main
   ```

#### What happens next on Render?
As configured in `deploy.py`, Render's build script runs the build command:
`pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`

When Render executes `python manage.py migrate` during the build phase:
1. It reads your new migration file `0006_add_colophon_page.py`.
2. It pushes the new `Colophon` page data directly into your remote Postgres production database.
3. The live website immediately starts serving `/colophon/` to visitors without any downtime or code changes.
