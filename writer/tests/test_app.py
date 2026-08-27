"""Pilot tests for the TUI.

Every test drives the real app through `run_test()` with a stubbed client:
nothing here opens a socket, and the drafts directory is always a tmp_path
pointed at by `LOG_DRAFTS_DIR`. The stub is `FakeSyncClient` (the sync
engine's own fake, with the server's real hash recipes) plus the publish
half, so startup runs the real sync against a real fake server rather
than a canned answer.
"""

import json

import pytest
from textual.widgets import Checkbox, Input, ListView, Static, TextArea

from writer import app as app_module
from writer.app import NewDraftModal, PublishModal, SidebarItem, StartServerModal, WriterApp
from writer.client import ClientError
from writer.sync import SyncReport, content_hash
from writer.tests.test_sync import FakeSyncClient

BEAR = (
    "title: bear\n"
    "slug: 230919-bear\n"
    "date: 2023-09-19 08:00\n"
    "\n"
    "The bear body.\n"
)

CROW = "title: crow\nslug: 231002-crow\n\nThe crow body.\n"

# The server's copy of the bear draft, byte-for-byte what BEAR parses to.
# An entry that agrees with its local file is adopted on the first sync,
# which is how a slug comes to have a base — and so how it comes to sit
# in the log section, unmarked, and to publish as an update.
BEAR_ENTRY = {
    "title": "bear",
    "content_markdown": "The bear body.\n",
    "publish_date": "2023-09-19 08:00",
}

# The same parsed fields as BEAR, written the way a person writes them.
# A sync that adopts this file rewrites it for formatting alone — which
# is still a rewrite, and so still answers to the open-editor guard.
SPACED_BEAR = (
    "title:   bear\n"
    "slug: 230919-bear\n"
    "date:  2023-09-19 08:00\n"
    "\n"
    "The bear body.\n"
)


class StubClient(FakeSyncClient):
    """The whole app-facing surface: the sync half, plus publish.

    `entries` keeps `FakeSyncClient`'s shape — slug -> {"title",
    "content_markdown", "publish_date"} — and `upsert_entry` writes into
    it, so a sync after a publish sees exactly what the publish sent, the
    way the real server would. `fail=True` models a server that cannot be
    reached at all. Never touches the network.
    """

    def __init__(
        self,
        entries=None,
        assets=None,
        fail=False,
        fail_assets_for=None,
        upload_error=None,
        upload_error_on=None,
        entry_upsert_error=None,
        entry_status="created",
    ):
        super().__init__(
            entries=entries, assets=assets, fail=fail, fail_assets_for=fail_assets_for
        )
        self._upload_error = upload_error
        self._upload_error_on = upload_error_on
        self._entry_upsert_error = entry_upsert_error
        self._entry_status = entry_status
        self.publish_calls = []

    def upload_asset(self, slug, name, data):
        self.publish_calls.append(("upload", name))
        # Server-faithful: a `LogAsset` is a foreign key onto a `LogEntry`
        # (website/models.py), so the real `/api/writer/assets` endpoint
        # 404s ("unknown entry") for a slug with no entry row yet. This
        # models that constraint — only entries the server already has, or
        # ones created via a successful `upsert_entry`, accept an upload —
        # so a publish that (re)introduces the assets-before-entry bug for
        # a new slug fails the same way the real server would.
        if slug not in self.entries:
            raise ClientError("404 unknown entry")
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
        self.entries[slug] = {
            "title": title,
            "content_markdown": content_markdown,
            "publish_date": publish_date,
        }
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
    """Let the sync worker — and anything it starts — finish, and the UI catch up.

    Looped, because a worker's result can start the next worker: a
    publish's success runs a sync, and one `wait_for_complete` only ever
    waits for the workers that existed when it was called.
    """
    for _ in range(3):
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


async def select_row(app, pilot, label):
    """Move the sidebar cursor onto the row with this exact label."""
    sidebar = app.query_one("#sidebar", ListView)
    sidebar.focus()
    for i, item in enumerate(sidebar.children):
        if isinstance(item, SidebarItem) and item.label == label:
            sidebar.index = i
            await pilot.pause()
            return
    raise AssertionError(f"no sidebar row labelled {label!r} in {labels(app)}")


# --- sidebar ------------------------------------------------------------

async def test_sidebar_lists_drafts_from_the_drafts_dir(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "○ 230919-bear" in labels(app)
        assert "○ 231002-crow" in labels(app)


async def test_sidebar_lists_mirrored_entries_under_the_divider(drafts):
    entries = {
        "220101-old": {
            "title": "old", "content_markdown": "The old body.\n", "publish_date": "2022-01-01",
        }
    }
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        rows = labels(app)
        assert "220101-old" in rows
        assert rows.index("○ 230919-bear") < rows.index("220101-old")


async def test_startup_sync_populates_the_log_section_with_markers(drafts):
    """One sync, three fates: adopted, brought down new, and in conflict."""
    (drafts / "240101-owl.md").write_text(
        "title: owl\nslug: 240101-owl\n\nThe owl body.\n", encoding="utf-8"
    )
    client = FakeSyncClient(entries={
        "230919-bear": dict(BEAR_ENTRY),
        # Same slug as a local draft, different body and no shared base:
        # neither side may be assumed right, so the row is marked.
        "231002-crow": {
            "title": "crow", "content_markdown": "A different crow.\n",
            "publish_date": "2023-10-02",
        },
        "231103-fox": {
            "title": "fox", "content_markdown": "The fox body.\n",
            "publish_date": "2023-11-03",
        },
    })
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)

        assert labels(app) == [
            "○ 240101-owl",
            "── log ──",
            "231103-fox",
            "230919-bear",
            "⚠ 231002-crow",
        ]
        assert app._entry_state("230919-bear") == "clean"
        assert app._entry_state("231002-crow") == "conflict"
        assert app._entry_state("240101-owl") == "local-only"
        # A conflict is marked, never resolved by overwriting: the local
        # file is exactly as the poet left it.
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == CROW
        assert any(m == "synced · 1 new · 1 conflict" for m in notifications(app))


async def test_startup_sync_writes_a_server_entry_into_a_local_file(drafts):
    """An entry the poet has no file for arrives as one, openable like any other."""
    entries = {
        "231103-fox": {
            "title": "fox", "content_markdown": "The fox body.\n",
            "publish_date": "2023-11-03",
        }
    }
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)

        path = drafts / "231103-fox.md"
        assert path.read_text(encoding="utf-8") == (
            "title: fox\nslug: 231103-fox\ndate: 2023-11-03\n\nThe fox body.\n"
        )
        assert "231103-fox" in labels(app)

        await select_row(app, pilot, "231103-fox")
        assert app.current_draft is not None
        assert app.current_draft.slug == "231103-fox"
        assert body_area(app).text == "The fox body.\n"


async def test_a_server_newer_entry_lands_on_disk_and_in_the_open_editor(drafts):
    """A clean editor is safe to update — but it has to be told."""
    client = StubClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        assert body_area(app).text == "The bear body.\n"

        client.entries["230919-bear"]["content_markdown"] = "A bear rewritten in the admin.\n"
        await pilot.press("ctrl+r")
        await settle(app, pilot)

        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == (
            "title: bear\nslug: 230919-bear\ndate: 2023-09-19 08:00\n"
            "\nA bear rewritten in the admin.\n"
        )
        assert body_area(app).text == "A bear rewritten in the admin.\n"
        assert app._entry_state("230919-bear") == "clean"
        assert app.suppressed_changes == 0
        assert any("1 updated" in m for m in notifications(app))


async def test_sync_never_rewrites_a_dirty_open_editor_and_marks_it_instead(drafts):
    """The open-editor guard: unsaved text outranks a server-newer entry."""
    client = StubClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # "xtitle: bear" — held in the editor, unsaved
        await pilot.pause()
        assert status(app).startswith("✗")

        client.entries["230919-bear"]["content_markdown"] = "A bear rewritten in the admin.\n"
        await pilot.press("ctrl+r")
        await settle(app, pilot)

        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == BEAR
        assert header_area(app).text.startswith("xtitle: bear")
        assert app._entry_state("230919-bear") == "conflict"
        assert "⚠ 230919-bear" in labels(app)


async def test_a_clean_editor_takes_up_what_the_adopt_rewrote(drafts):
    """The pass-through side: a clean editor still follows the rewrite."""
    path = drafts / "230919-bear.md"
    path.write_text(SPACED_BEAR, encoding="utf-8")
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)

        assert path.read_text(encoding="utf-8") == BEAR
        assert header_area(app).text == (
            "title: bear\nslug: 230919-bear\ndate: 2023-09-19 08:00"
        )
        assert app.suppressed_changes == 0


async def test_sync_never_rewrites_a_dirty_open_editor_on_the_adopt_path(drafts):
    """The guard covers the canonicalizing rewrite too, at both layers.

    ADVANCE_BASE writes the file for header formatting alone, and the
    app takes up whatever a sync wrote — so without the guard on both,
    unsaved text is replaced by a rewrite that changed nothing anyone
    asked to change.
    """
    path = drafts / "230919-bear.md"
    path.write_text(SPACED_BEAR, encoding="utf-8")
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.current_path == path
        assert path.read_text(encoding="utf-8") == BEAR  # the clean adopt ran

        # Put the file back as it was written and forget the base, so the
        # next sync reaches the same adopt all over again.
        path.write_text(SPACED_BEAR, encoding="utf-8")
        (drafts / ".sync.json").unlink()

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # unsaved and unparseable: nothing reaches disk
        await pilot.pause()
        assert status(app).startswith("✗")

        await pilot.press("ctrl+r")
        await settle(app, pilot)

        assert path.read_text(encoding="utf-8") == SPACED_BEAR
        assert header_area(app).text.startswith("xtitle: bear")
        assert status(app).startswith("✗")
        # The base advances regardless: the file already says what the
        # server says, whatever the editor is still holding.
        assert app._entry_state("230919-bear") == "clean"


async def test_the_editor_reload_answers_to_the_guard_too(drafts):
    """Defense in depth, pinned directly.

    With the engine guarding its own writes, no ordinary sync can hand
    the app a report that says the open slug was rewritten while its
    editor was dirty — so this drives `_sync_arrived` itself, the way
    the guard's second layer would be reached if the first ever slipped.
    Reloading is a write onto the editor, and it answers to the same
    verdict every other write does.
    """
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        path = drafts / "230919-bear.md"
        assert app.current_path == path
        before = body_area(app).text

        path.write_text(
            "title: bear\nslug: 230919-bear\n\nSomething else entirely.\n",
            encoding="utf-8",
        )
        report = SyncReport(adopted=["230919-bear"])

        await app._sync_arrived(report, "230919-bear", False)
        await pilot.pause()
        assert body_area(app).text == before

        await app._sync_arrived(report, "230919-bear", True)
        await pilot.pause()
        assert body_area(app).text == "Something else entirely.\n"


async def test_a_conflict_with_no_base_survives_an_offline_restart(drafts):
    """The ⚠ is in the sidecar, so it outlives the session that found it."""
    entries = {
        "231002-crow": {
            "title": "crow", "content_markdown": "A different crow.\n",
            "publish_date": "2023-10-02",
        }
    }
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "⚠ 231002-crow" in labels(app)

    # A second session with no server to ask: the sidecar is all there is.
    offline = WriterApp(client=StubClient(fail=True))
    async with offline.run_test() as pilot:
        await settle(offline, pilot)
        assert "⚠ 231002-crow" in labels(offline)
        assert offline._entry_state("231002-crow") == "conflict"
        # And it is still known to be the server's — what the new-draft
        # clash check and the publish refusal both read.
        assert "231002-crow" in offline.published_slugs


async def test_an_edit_marks_the_sidecar_on_disk_and_survives_a_restart(
    drafts, monkeypatch
):
    """The ● is written through, and only on the transition."""
    writes = []
    real_save_state = app_module.save_state

    def spy(path, state):
        writes.append(path)
        return real_save_state(path, state)

    monkeypatch.setattr(app_module, "save_state", spy)

    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")

        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()

        on_disk = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert on_disk["230919-bear"]["state"] == "edited"
        # Only the label moves — the base itself is the sync's to advance.
        assert on_disk["230919-bear"]["hash"] == content_hash("bear", "The bear body.\n")
        assert on_disk["230919-bear"]["date"] == "2023-09-19 08:00"

        # A second keystroke is not a second transition.
        await pilot.press("?")
        await pilot.pause()
        assert len(writes) == 1

    offline = WriterApp(client=StubClient(fail=True))
    async with offline.run_test() as pilot:
        await settle(offline, pilot)
        assert "● 230919-bear" in labels(offline)
        assert offline._entry_state("230919-bear") == "edited"


async def test_an_edited_mirrored_entry_shows_its_marker_at_once(drafts):
    """● the moment the file stops matching its base — no round trip."""
    client = StubClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        assert "230919-bear" in labels(app)

        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()

        assert "● 230919-bear" in labels(app)
        assert app._entry_state("230919-bear") == "edited"
        assert client.calls["list_entries_full"] == 1  # nothing was asked of the server

        # And back: an edit undone is not an edit.
        await pilot.press("backspace")
        await pilot.pause()
        assert "230919-bear" in labels(app)
        assert app._entry_state("230919-bear") == "clean"


async def test_offline_startup_is_quiet_and_still_editable(drafts):
    """Failure rule 7: offline says so once, in the sidebar, and gets out of the way."""
    app = WriterApp(client=StubClient(fail=True))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert notifications(app) == []
        assert "(offline)" in labels(app)

        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert status(app).startswith("✓")
        assert app.is_running

    assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == (
        "title: bear\n"
        "slug: 230919-bear\n"
        "date: 2023-09-19 08:00\n"
        "\n"
        "!The bear body.\n"
    )


async def test_offline_client_still_runs_and_shows_offline(drafts):
    app = WriterApp(client=StubClient(fail=True))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "(offline)" in labels(app)
        # The drafts half of the sidebar is unaffected, and the app is alive.
        assert "○ 230919-bear" in labels(app)
        assert app.is_running


async def test_an_asset_failure_is_reported_and_the_sync_carries_on(drafts):
    """An entry lands even when its images don't — and the reason is said."""
    client = FakeSyncClient(
        entries={
            "231103-fox": {
                "title": "fox", "content_markdown": "The fox body.\n",
                "publish_date": "2023-11-03",
            }
        },
        assets={"231103-fox": {"pic.png": b"pixels"}},
        fail_assets_for={"231103-fox"},
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)

        assert (drafts / "231103-fox.md").exists()
        assert any("231103-fox" in m and "offline" in m for m in notifications(app))
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


async def test_a_mirrored_row_opens_its_local_file(drafts):
    """Both sections open the same way: there is nothing here to pull."""
    client = StubClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        assert app.current_draft.slug == "230919-bear"
        assert app.current_path == drafts / "230919-bear.md"
        assert body_area(app).text == "The bear body.\n"


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
    """ctrl+r, and the sync landing with it, must not flatter the gauge."""
    original = (drafts / "230919-bear.md").read_text(encoding="utf-8")
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
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
        def list_entries_full(self):
            raise KeyError("entries")

    app = WriterApp(client=Broken())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "(offline)" in labels(app)
        assert "○ 230919-bear" in labels(app)
        assert app.is_running


# --- status line --------------------------------------------------------

async def test_status_counts_words_and_marks_entries_on_the_server(drafts):
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        assert "3 words" in status(app)
        assert "on server" in status(app)

        await select_row(app, pilot, "○ 231002-crow")
        assert "on server" not in status(app)


# --- injection ----------------------------------------------------------

async def test_client_is_only_asked_for_entries_once_per_sync(drafts):
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert client.calls["list_entries_full"] == 1


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
        assert "○ 231103-fox" in labels(app)
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


async def test_new_draft_refuses_a_slug_the_server_already_has(drafts):
    """The mirrored file can be deleted; the entry it stood for is still there.

    The sidecar is what remembers, so the clash is caught by the base
    rather than by the file — which is exactly the case a mirror has to
    get right, since the next sync would bring that entry back.
    """
    entries = {
        "230101-old": {
            "title": "old", "content_markdown": "The old body.\n",
            "publish_date": "2022-01-01",
        }
    }
    app = WriterApp(client=StubClient(entries=entries))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        (drafts / "230101-old.md").unlink()

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#title", Input).value = "old"
        app.screen.query_one("#slug", Input).value = "230101-old"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, NewDraftModal)
        assert "published" in str(app.screen.query_one("#error", Static).content)
        assert not (drafts / "230101-old.md").exists()


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


async def test_publish_modal_shows_update_statement_for_a_mirrored_slug(drafts, monkeypatch):
    """The statement comes from the sidecar's base, not from a fetch cache."""
    local_env(monkeypatch)
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        await pilot.press("ctrl+b")
        await pilot.pause()
        statement = str(app.screen.query_one("#statement", Static).content)
        assert statement == 'updates "bear" from 2023'


async def test_publish_create_case_upserts_the_entry_before_uploading_assets(drafts, monkeypatch):
    """A brand-new slug: the server has no entry row yet, so an asset upload
    would 404 ("unknown entry") if attempted first — `upsert_entry` (which
    creates that row) must run before any asset does.
    """
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient()  # entries={} — "230919-bear" is not yet published
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert client.publish_calls == [
            (
                "upsert",
                "230919-bear",
                "bear",
                "The bear body.\n",
                "2023-09-19 08:00",
                False,
                False,
            ),
            ("upload", "a.png"),
            ("upload", "b.png"),
        ]
        assert not isinstance(app.screen, PublishModal)
        assert any("published 230919-bear (created)" in m for m in notifications(app))


async def test_publish_update_case_uploads_assets_before_upserting_the_entry(drafts, monkeypatch):
    """An already-published slug keeps assets-first: the live page must
    never end up with markdown that references an asset that hasn't
    landed yet, and the entry row already exists so nothing 404s.
    """
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}, entry_status="updated")
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
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
        assert any("published 230919-bear (updated)" in m for m in notifications(app))


async def test_publish_regression_new_slug_with_assets_converges_instead_of_404ing(
    drafts, monkeypatch
):
    """Pins the bug this round fixed: assets-first for a brand-new slug 404s.

    First proves the stub really is server-faithful — calling
    `upload_asset` for a slug with no entry yet raises the same "404
    unknown entry" truth the real server would (`website/api.py`'s
    `assets` view, backed by `LogAsset.log_entry`'s foreign key in
    `website/models.py`) — the shape of the original failure. Then drives
    the actual app through `ctrl+b` for that same slug and confirms it now
    converges to a clean success, because `_publish_create` upserts first.
    """
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    probe = StubClient()
    with pytest.raises(ClientError, match="404"):
        probe.upload_asset("230919-bear", "a.png", b"aaa")

    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert any("published 230919-bear (created)" in m for m in notifications(app))
        assert not any("404" in m for m in notifications(app))


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


async def test_publish_update_case_asset_conflict_names_uploaded_files_and_skips_the_entry(
    drafts, monkeypatch
):
    """Update case: an asset conflict must never reach the entry at all —
    assets still go first here, so this failure happens before any upsert.
    """
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient(
        entries={"230919-bear": dict(BEAR_ENTRY)},
        upload_error="409 b.png exists with different content",
        upload_error_on="b.png",
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
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


async def test_publish_update_case_entry_failure_after_uploads_lists_uploaded_names(
    drafts, monkeypatch
):
    """Update case: assets land fine, then the upsert itself fails."""
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    client = StubClient(
        entries={"230919-bear": dict(BEAR_ENTRY)}, entry_upsert_error="500 internal error"
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        messages = notifications(app)
        assert any(
            "500" in m and "a.png" in m and "re-publish" in m.lower() for m in messages
        )


async def test_publish_create_case_asset_failure_after_successful_upsert(drafts, monkeypatch):
    """Create case: the entry lands, then an asset fails.

    Unlike the update case, the entry already exists on the server by this
    point regardless — the message must say so (created/updated), name
    what landed and what didn't, and say re-publishing is safe (a retry
    now takes the update path, which converges).
    """
    local_env(monkeypatch)
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"aaa")
    (assets / "b.png").write_bytes(b"bbb")
    client = StubClient(  # entries={} — a new slug, so this is the create case
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

        assert client.publish_calls == [
            (
                "upsert",
                "230919-bear",
                "bear",
                "The bear body.\n",
                "2023-09-19 08:00",
                False,
                False,
            ),
            ("upload", "a.png"),
            ("upload", "b.png"),
        ]
        messages = notifications(app)
        assert any(
            "created" in m
            and "a.png" in m
            and "b.png" in m
            and "409" in m
            and "re-publish" in m.lower()
            for m in messages
        )


async def test_publish_success_notifies_and_syncs_the_entry_into_the_log(drafts, monkeypatch):
    local_env(monkeypatch)
    client = StubClient()
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert client.calls["list_entries_full"] == 1
        assert "○ 230919-bear" in labels(app)

        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert any("published 230919-bear (created)" in m for m in notifications(app))
        assert client.calls["list_entries_full"] == 2
        # The row leaves the drafts section for the log, unmarked.
        assert "230919-bear" in labels(app)
        assert app._entry_state("230919-bear") == "clean"


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
