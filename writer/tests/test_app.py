"""Pilot tests for the TUI.

Every test drives the real app through `run_test()` with a stubbed client:
nothing here opens a socket, and the drafts directory is always a tmp_path
pointed at by `LOG_DRAFTS_DIR`.
"""

import pytest
from textual.widgets import ListView, Static, TextArea

from writer.app import WriterApp
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

    def __init__(self, entries=None, error=None):
        self._entries = entries if entries is not None else []
        self._error = error
        self.calls = 0

    def list_entries(self):
        self.calls += 1
        if self._error is not None:
            raise ClientError(self._error)
        return list(self._entries)


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


def body_area(app):
    return app.query_one("#body", TextArea)


def header_area(app):
    return app.query_one("#header", TextArea)


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
