"""Pilot tests for the TUI.

Every test drives the real app through `run_test()` with a stubbed client:
nothing here opens a socket, and the drafts directory is always a tmp_path
pointed at by `LOG_DRAFTS_DIR`.
"""

import pytest
from textual.widgets import Checkbox, Input, ListView, Static, TextArea

from writer.app import NewDraftModal, PublishModal, SidebarItem, StartServerModal, WriterApp
from writer.client import ClientError

BEAR = (
    "title: bear\n"
    "slug: 230919-bear\n"
    "date: 2023-09-19 08:00\n"
    "\n"
    "The bear body.\n"
)

CROW = "title: crow\nslug: 231002-crow\n\nThe crow body.\n"


class StubClient:
    """Stands in for `WriterClient`. Never touches the network."""

    def __init__(
        self,
        entries=None,
        error=None,
        entry_detail=None,
        entry_error=None,
        upload_error=None,
        upload_error_on=None,
        entry_upsert_error=None,
        entry_status="created",
    ):
        self._entries = entries if entries is not None else []
        self._error = error
        self._entry_detail = entry_detail or {}
        self._entry_error = entry_error
        self._upload_error = upload_error
        self._upload_error_on = upload_error_on
        self._entry_upsert_error = entry_upsert_error
        self._entry_status = entry_status
        self.calls = 0
        self.get_entry_calls = []
        self.publish_calls = []

    def list_entries(self):
        self.calls += 1
        if self._error is not None:
            raise ClientError(self._error)
        return list(self._entries)

    def get_entry(self, slug):
        self.get_entry_calls.append(slug)
        if self._entry_error is not None:
            raise ClientError(self._entry_error)
        return self._entry_detail[slug]

    def upload_asset(self, slug, name, data):
        self.publish_calls.append(("upload", name))
        if self._upload_error is not None and (
            self._upload_error_on is None or name == self._upload_error_on
        ):
            raise ClientError(self._upload_error)
        return {"status": "uploaded"}

    def upsert_entry(
        self,
        slug,
        title,
        content_markdown,
        publish_date=None,
        share_bluesky=False,
        share_mastodon=False,
    ):
        self.publish_calls.append(
            ("upsert", slug, title, content_markdown, publish_date, share_bluesky, share_mastodon)
        )
        if self._entry_upsert_error is not None:
            raise ClientError(self._entry_upsert_error)
        return {"status": self._entry_status, "slug": slug}


class StubProber:
    """Stands in for the preview port probe. Never opens a socket."""

    def __init__(self, up):
        self._up = up
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._up


class StubBrowser:
    """Stands in for `webbrowser.open`. Never launches a real browser."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)


class StubProcess:
    """Stands in for a `subprocess.Popen` handle."""

    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


class StubServerStarter:
    """Stands in for launching the dev server. Never starts a real process."""

    def __init__(self, process=None):
        self.process = process if process is not None else StubProcess()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.process


@pytest.fixture
def drafts(tmp_path, monkeypatch):
    """Two drafts in a tmp drafts directory."""
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    (tmp_path / "230919-bear.md").write_text(BEAR, encoding="utf-8")
    (tmp_path / "231002-crow.md").write_text(CROW, encoding="utf-8")
    return tmp_path


async def settle(app, pilot):
    """Let the published-entries worker finish and the UI catch up."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def labels(app):
    return [item.label for item in app.query_one("#sidebar", ListView).children]


def status(app):
    return str(app.query_one("#status", Static).content)


def notifications(app):
    """Messages of every notification `self.notify(...)` has raised so far."""
    return [n.message for n in app._notifications]


def body_area(app):
    return app.query_one("#body", TextArea)


def header_area(app):
    return app.query_one("#header", TextArea)


async def select_published(app, pilot, slug):
    """Move the sidebar cursor to the published row for `slug`."""
    sidebar = app.query_one("#sidebar", ListView)
    sidebar.focus()
    for _ in range(len(sidebar.children)):
        item = sidebar.highlighted_child
        if isinstance(item, SidebarItem) and item.kind == "published" and item.slug == slug:
            return
        await pilot.press("down")
        await pilot.pause()
    raise AssertionError(f"could not highlight published row {slug!r}")


# --- sidebar ------------------------------------------------------------

async def test_sidebar_lists_drafts_from_the_drafts_dir(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "230919-bear" in labels(app)
        assert "231002-crow" in labels(app)


async def test_sidebar_lists_published_entries_under_the_divider(drafts):
    entries = [{"slug": "220101-old", "title": "old", "publish_date": "2022-01-01"}]
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        rows = labels(app)
        assert "220101-old" in rows
        assert rows.index("230919-bear") < rows.index("220101-old")


async def test_offline_client_still_runs_and_shows_offline(drafts):
    app = WriterApp(client=StubClient(error="cannot reach example.test"))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "(offline)" in labels(app)
        # The drafts half of the sidebar is unaffected, and the app is alive.
        assert "230919-bear" in labels(app)
        assert app.is_running


async def test_an_empty_drafts_dir_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.current_draft is None
        assert body_area(app).text == ""
        assert app.is_running


# --- selection ----------------------------------------------------------

async def test_first_draft_is_loaded_on_start(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert body_area(app).text == "The bear body.\n"
        assert app.current_draft.slug == "230919-bear"


async def test_selecting_a_draft_loads_its_body_and_header(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#sidebar", ListView).focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.current_draft.slug == "231002-crow"
        assert body_area(app).text == "The crow body.\n"
        assert header_area(app).text == "title: crow\nslug: 231002-crow"


# --- autosave -----------------------------------------------------------

async def test_editing_the_body_autosaves_to_disk(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert status(app).startswith("✓")

    assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == (
        "title: bear\n"
        "slug: 230919-bear\n"
        "date: 2023-09-19 08:00\n"
        "\n"
        "!The bear body.\n"
    )


async def test_loading_a_draft_does_not_itself_save(drafts):
    """Programmatic population is suppressed — the file is untouched by a load."""
    before = (drafts / "231002-crow.md").stat().st_mtime_ns
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#sidebar", ListView).focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.suppressed_changes == 0
    assert (drafts / "231002-crow.md").stat().st_mtime_ns == before


async def test_bad_header_shows_error_and_leaves_the_file_untouched(drafts):
    original = (drafts / "230919-bear.md").read_text(encoding="utf-8")
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # "xtitle: bear" — no title key any more
        await pilot.pause()
        assert status(app).startswith("✗")
        assert "title" in status(app)

    assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == original


async def test_a_rebuild_does_not_paint_a_tick_over_a_parse_error(drafts):
    """ctrl+r, and the fetch landing with it, must not flatter the gauge."""
    original = (drafts / "230919-bear.md").read_text(encoding="utf-8")
    app = WriterApp(client=StubClient(entries=[{"slug": "230919-bear"}]))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")
        await pilot.pause()
        assert status(app).startswith("✗")

        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert status(app).startswith("✗")
        assert header_area(app).text.startswith("xtitle: bear")

    assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == original


async def test_a_broken_file_opens_so_that_it_can_be_fixed(drafts):
    (drafts / "230919-bear.md").write_text(
        "title: bear\nno colon here\n\nbody\n", encoding="utf-8"
    )
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert status(app).startswith("✗")
        assert app.current_draft is None
        assert "no colon here" in header_area(app).text
        assert body_area(app).text == "body\n"

        # Fixing it saves it back to the file it came from, not to a new one.
        area = header_area(app)
        area.focus()
        area.text = "title: bear\nslug: 230919-bear"
        await pilot.pause()
        assert status(app).startswith("✓")

    assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == (
        "title: bear\nslug: 230919-bear\n\nbody\n"
    )


# --- unknown header keys ------------------------------------------------

async def test_an_unknown_header_key_is_visible_and_survives_an_edit(drafts):
    path = drafts / "230919-bear.md"
    path.write_text(
        "title: bear\nslug: 230919-bear\nmood: blue\n\nThe bear body.\n",
        encoding="utf-8",
    )
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "mood: blue" in header_area(app).text
        assert "mood" in status(app)

        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert "mood" in status(app)

    assert path.read_text(encoding="utf-8") == (
        "title: bear\nslug: 230919-bear\nmood: blue\n\n!The bear body.\n"
    )


async def test_the_meta_pane_shows_the_files_own_header_bytes(drafts):
    """Order and spacing as written, not as a serializer would write them."""
    header = "title:   bear\nmood: blue\nslug: 230919-bear"
    (drafts / "230919-bear.md").write_text(
        f"{header}\n\nThe bear body.\n", encoding="utf-8"
    )
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert header_area(app).text == header


# --- a client that misbehaves -------------------------------------------

async def test_a_client_breaking_its_own_contract_is_still_only_offline(drafts):
    class Broken:
        def list_entries(self):
            raise KeyError("entries")

    app = WriterApp(client=Broken())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "(offline)" in labels(app)
        assert "230919-bear" in labels(app)
        assert app.is_running


# --- status line --------------------------------------------------------

async def test_status_counts_words_and_marks_entries_on_the_server(drafts):
    entries = [{"slug": "230919-bear", "title": "bear", "publish_date": "2023-09-19"}]
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "3 words" in status(app)
        assert "on server" in status(app)

        app.query_one("#sidebar", ListView).focus()
        await pilot.press("down")
        await pilot.pause()
        assert "on server" not in status(app)


# --- injection ----------------------------------------------------------

async def test_client_is_only_asked_for_entries_once_per_fetch(drafts):
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert client.calls == 1


# --- a file removed underneath the sidebar -------------------------------

async def test_load_draft_into_editor_survives_a_file_removed_underneath_it(drafts):
    """A rebuild racing an external deletion must not crash the app."""
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        missing = drafts / "231002-crow.md"
        missing.unlink()
        app.load_draft_into_editor(missing)
        await pilot.pause()
        assert status(app).startswith("✗")
        assert "cannot read" in status(app)
        assert app.current_draft is None
        assert app.is_running


# --- new draft ------------------------------------------------------------

async def test_new_draft_modal_creates_file_and_assets_dir_and_selects_it(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, NewDraftModal)

        app.screen.query_one("#title", Input).value = "fox"
        app.screen.query_one("#slug", Input).value = "231103-fox"
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert (drafts / "231103-fox.md").exists()
        assert (drafts / "231103-fox.assets").is_dir()
        assert app.current_draft is not None
        assert app.current_draft.slug == "231103-fox"
        assert "231103-fox" in labels(app)
        assert not isinstance(app.screen, NewDraftModal)


async def test_new_draft_refuses_a_blank_title(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+n")
        await pilot.pause()

        app.screen.query_one("#slug", Input).value = "231103-fox"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, NewDraftModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert not (drafts / "231103-fox.md").exists()


async def test_new_draft_refuses_a_bad_slug_and_keeps_the_modal_open(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+n")
        await pilot.pause()

        app.screen.query_one("#title", Input).value = "fox"
        app.screen.query_one("#slug", Input).value = "Not A Slug!"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, NewDraftModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert not any(
            p.name.startswith("Not A Slug") for p in drafts.iterdir()
        )


async def test_new_draft_refuses_an_existing_draft_file_and_keeps_the_modal_open(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        original = (drafts / "230919-bear.md").read_text(encoding="utf-8")

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#title", Input).value = "another bear"
        app.screen.query_one("#slug", Input).value = "230919-bear"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, NewDraftModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == original


async def test_new_draft_refuses_a_slug_clashing_with_a_published_entry(drafts):
    entries = [{"slug": "230101-old", "title": "old", "publish_date": "2022-01-01"}]
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#title", Input).value = "old"
        app.screen.query_one("#slug", Input).value = "230101-old"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, NewDraftModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert not (drafts / "230101-old.md").exists()


# --- pull to draft ----------------------------------------------------------

async def test_pull_to_draft_writes_the_expected_file_and_selects_it(drafts):
    entries = [{"slug": "231103-fox", "title": "fox", "publish_date": "2023-11-03"}]
    detail = {
        "231103-fox": {
            "title": "fox",
            "content_markdown": "The fox body.\n",
            "publish_date": "2023-11-03",
        }
    }
    client = StubClient(entries=entries, entry_detail=detail)
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_published(app, pilot, "231103-fox")

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        path = drafts / "231103-fox.md"
        assert path.read_text(encoding="utf-8") == (
            "title: fox\nslug: 231103-fox\ndate: 2023-11-03\n\nThe fox body.\n"
        )
        assert app.current_draft is not None
        assert app.current_draft.slug == "231103-fox"
        assert "231103-fox" in labels(app)


async def test_pull_to_draft_refuses_to_overwrite_an_existing_draft(drafts):
    entries = [{"slug": "230919-bear", "title": "bear", "publish_date": "2023-09-19"}]
    detail = {
        "230919-bear": {
            "title": "a very different bear",
            "content_markdown": "Not the same bear at all.\n",
            "publish_date": "2023-09-19",
        }
    }
    client = StubClient(entries=entries, entry_detail=detail)
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        before = (drafts / "230919-bear.md").read_text(encoding="utf-8")
        await select_published(app, pilot, "230919-bear")

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        assert client.get_entry_calls == ["230919-bear"]
        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == before
        assert "exists" in status(app) or "overwrite" in status(app)


async def test_pull_to_draft_reports_a_client_error_and_carries_on(drafts):
    entries = [{"slug": "231103-fox", "title": "fox", "publish_date": "2023-11-03"}]
    client = StubClient(entries=entries, entry_error="401 unauthorized")
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_published(app, pilot, "231103-fox")

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        assert not (drafts / "231103-fox.md").exists()
        assert "401 unauthorized" in status(app)
        assert app.is_running


# --- preview ----------------------------------------------------------------

async def test_preview_opens_the_browser_when_the_server_is_already_up(drafts):
    prober = StubProber(up=True)
    browser = StubBrowser()
    starter = StubServerStarter()
    app = WriterApp(
        client=StubClient(), prober=prober, browser_opener=browser, start_server=starter
    )
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+l")
        await pilot.pause()

        assert prober.calls == 1
        assert browser.calls == ["http://127.0.0.1:8000/draft-preview/230919-bear/"]
        assert starter.calls == 0
        assert app._server_process is None


async def test_preview_offers_to_start_the_server_when_it_is_down(drafts):
    prober = StubProber(up=False)
    browser = StubBrowser()
    process = StubProcess()
    starter = StubServerStarter(process)
    app = WriterApp(
        client=StubClient(), prober=prober, browser_opener=browser, start_server=starter
    )
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+l")
        await pilot.pause()

        assert isinstance(app.screen, StartServerModal)
        assert starter.calls == 0

        await pilot.click("#confirm")
        await pilot.pause()

        assert starter.calls == 1
        assert app._server_process is process
        assert browser.calls == ["http://127.0.0.1:8000/draft-preview/230919-bear/"]


async def test_preview_declining_to_start_the_server_starts_nothing(drafts):
    prober = StubProber(up=False)
    browser = StubBrowser()
    starter = StubServerStarter()
    app = WriterApp(
        client=StubClient(), prober=prober, browser_opener=browser, start_server=starter
    )
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+l")
        await pilot.pause()

        assert isinstance(app.screen, StartServerModal)
        await pilot.click("#cancel")
        await pilot.pause()

        assert starter.calls == 0
        assert browser.calls == []
        assert app._server_process is None


# --- publish ------------------------------------------------------------

def local_env(monkeypatch):
    """A local, DEBUG-server base URL — publishing needs no token against it."""
    monkeypatch.setenv("BLOG_WRITER_BASE_URL", "http://127.0.0.1:9000")
    monkeypatch.delenv("BLOG_WRITER_TOKEN", raising=False)


async def test_publish_modal_shows_create_statement_for_a_new_slug(drafts, monkeypatch):
    local_env(monkeypatch)
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, PublishModal)
        statement = str(app.screen.query_one("#statement", Static).content)
        assert statement == 'creates new entry "230919-bear"'


async def test_publish_modal_shows_update_statement_for_a_published_slug(drafts, monkeypatch):
    local_env(monkeypatch)
    entries = [{"slug": "230919-bear", "title": "old bear", "publish_date": "2022-01-01"}]
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        statement = str(app.screen.query_one("#statement", Static).content)
        assert statement == 'updates "old bear" from 2022'


async def test_publish_uploads_assets_before_upserting_the_entry(drafts, monkeypatch):
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert client.publish_calls == [
            ("upload", "a.png"),
            ("upload", "b.png"),
            (
                "upsert",
                "230919-bear",
                "bear",
                "The bear body.\n",
                "2023-09-19 08:00",
                False,
                False,
            ),
        ]
        assert not isinstance(app.screen, PublishModal)
        assert any("published 230919-bear (created)" in m for m in notifications(app))


async def test_publish_share_flags_are_passed_only_when_checked(drafts, monkeypatch):
    local_env(monkeypatch)
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        app.screen.query_one("#bluesky", Checkbox).value = True
        app.screen.query_one("#mastodon", Checkbox).value = True
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        upsert_calls = [c for c in client.publish_calls if c[0] == "upsert"]
        assert len(upsert_calls) == 1
        assert upsert_calls[0][-2:] == (True, True)


async def test_publish_default_checkboxes_are_off(drafts, monkeypatch):
    local_env(monkeypatch)
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert app.screen.query_one("#bluesky", Checkbox).value is False
        assert app.screen.query_one("#mastodon", Checkbox).value is False


async def test_publish_stops_when_token_is_missing_against_a_remote_server(drafts, monkeypatch):
    monkeypatch.setenv("BLOG_WRITER_BASE_URL", "https://example.test")
    monkeypatch.delenv("BLOG_WRITER_TOKEN", raising=False)
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, PublishModal)
        assert "BLOG_WRITER_TOKEN is not set" in str(
            app.screen.query_one("#error", Static).content
        )
        assert client.publish_calls == []


async def test_publish_asset_conflict_names_uploaded_files_and_skips_the_entry(
    drafts, monkeypatch
):
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient(
        upload_error="409 b.png exists with different content", upload_error_on="b.png"
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert client.publish_calls == [("upload", "a.png"), ("upload", "b.png")]
        assert not any(c[0] == "upsert" for c in client.publish_calls)
        messages = notifications(app)
        assert any(
            "409" in m and "a.png" in m and "re-publish" in m.lower() for m in messages
        )


async def test_publish_entry_failure_after_uploads_lists_uploaded_names(drafts, monkeypatch):
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    client = StubClient(entry_upsert_error="500 internal error")
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        messages = notifications(app)
        assert any(
            "500" in m and "a.png" in m and "re-publish" in m.lower() for m in messages
        )


async def test_publish_success_notifies_and_refreshes_the_published_list(drafts, monkeypatch):
    local_env(monkeypatch)
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert client.calls == 1

        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert any("published 230919-bear (created)" in m for m in notifications(app))
        assert client.calls == 2


async def test_publish_cancel_does_nothing(drafts, monkeypatch):
    local_env(monkeypatch)
    original = (drafts / "230919-bear.md").read_text(encoding="utf-8")
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
        await settle(app, pilot)

        assert not isinstance(app.screen, PublishModal)
        assert client.publish_calls == []
        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == original


async def test_server_process_is_terminated_on_app_exit(drafts):
    prober = StubProber(up=False)
    process = StubProcess()
    starter = StubServerStarter(process)
    app = WriterApp(
        client=StubClient(),
        prober=prober,
        browser_opener=StubBrowser(),
        start_server=starter,
    )
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+l")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        assert app._server_process is process

    assert process.terminated
