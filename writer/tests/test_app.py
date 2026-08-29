"""Pilot tests for the TUI.

Every test drives the real app through `run_test()` with a stubbed client:
nothing here opens a socket, and the drafts directory is always a tmp_path
pointed at by `LOG_DRAFTS_DIR`. The stub is `FakeSyncClient` (the sync
engine's own fake, with the server's real hash recipes) plus the publish
half, so startup runs the real sync against a real fake server rather
than a canned answer.
"""

import json
import threading

import pytest
from textual.widgets import Checkbox, Input, ListView, Static, TextArea

from writer import app as app_module
from writer.app import (
    AddImageModal,
    ConflictModal,
    NewDraftModal,
    PublishModal,
    SidebarItem,
    StartServerModal,
    WriterApp,
)
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

# The server's copy of the crow draft, likewise byte-for-byte what CROW
# parses to — a mirrored entry that carries no date.
CROW_ENTRY = {
    "title": "crow",
    "content_markdown": "The crow body.\n",
    "publish_date": None,
}

# A server-side crow that diverges from CROW in body — a first-run
# conflict, since neither side may be assumed right and there is no
# recorded base to compare against. `sings differently` is a phrase
# that appears only on this side, for the diff-view tests.
CROW_SERVER = {
    "title": "crow",
    "content_markdown": "A crow that sings differently on the server.\n",
    "publish_date": "2023-10-02",
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
        fail_get_entry_for=None,
    ):
        super().__init__(
            entries=entries, assets=assets, fail=fail, fail_assets_for=fail_assets_for
        )
        self._upload_error = upload_error
        self._upload_error_on = upload_error_on
        self._entry_upsert_error = entry_upsert_error
        self._entry_status = entry_status
        self._fail_get_entry_for = fail_get_entry_for if fail_get_entry_for is not None else set()
        self.publish_calls = []

    def get_entry(self, slug):
        self.calls["get_entry"] = self.calls.get("get_entry", 0) + 1
        if self.fail or slug in self._fail_get_entry_for:
            raise ClientError("offline")
        e = self.entries[slug]
        return {
            "slug": slug,
            "title": e["title"],
            "content_markdown": e["content_markdown"],
            "publish_date": e["publish_date"],
        }

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

    Not safe to call while a modal is deliberately left open: the
    asyncio-flavored worker hosting `push_screen_wait` (e.g.
    `_open_conflict`) only completes once that modal dismisses, so a
    plain `wait_for_complete()` blocks forever against a click that is
    expected to leave the modal up (a fetch failure, or `view diff`).
    Use `settle_conflict_fetch` for those.
    """
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def settle_conflict_fetch(app, pilot):
    """Wait for the conflict modal's own fetch/write worker, not the modal.

    `ConflictModal`'s keep-mine/take-server/view-diff buttons each start
    a `group="conflict"` thread worker and return at once; the modal
    stays open until that worker calls back. The modal's own hosting
    worker (`_open_conflict`, plain `@work`, default group) only
    completes once the modal dismisses — which a fetch failure or "view
    diff" never does — so waiting on it too, the way `settle` does,
    would hang. This waits only for the `"conflict"`-group worker(s).
    """
    workers = [w for w in app.workers if w.group == "conflict"]
    if workers:
        await app.workers.wait_for_complete(workers)
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
        await select_row(app, pilot, "230919-bear")

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
        await select_row(app, pilot, "230919-bear")
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


async def test_the_editor_reload_answers_to_its_own_baseline(drafts):
    """A reload never carries off text nobody has saved.

    Driven through `_sync_arrived` directly, because what is under test
    is the editor open when a sync *lands* — including the case the
    engine's own guard cannot cover, a row switched into while the
    worker ran. The verdict the guard measured at sync start is passed
    as `True` throughout: it is not what the answer turns on.
    """
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        path = drafts / "230919-bear.md"
        assert app.current_path == path

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # unsaved: the editors are off their baseline
        await pilot.pause()

        path.write_text(
            "title: bear\nslug: 230919-bear\n\nSomething else entirely.\n",
            encoding="utf-8",
        )
        report = SyncReport(updated=["230919-bear"])
        await app._sync_arrived(report, "230919-bear", True)
        await pilot.pause()

        assert header_area(app).text.startswith("xtitle: bear")
        assert body_area(app).text == "The bear body.\n"

        # Reopening the row is the deliberate answer to that; back on
        # their baseline, the editors take up the next rewrite as usual.
        app.load_draft_into_editor(path)
        await pilot.pause()
        path.write_text(
            "title: bear\nslug: 230919-bear\n\nNewer still.\n", encoding="utf-8"
        )
        await app._sync_arrived(report, "230919-bear", True)
        await pilot.pause()
        assert body_area(app).text == "Newer still.\n"


async def test_a_row_switched_into_mid_sync_still_takes_up_what_the_sync_wrote(drafts):
    """The sync started on one row and landed on another.

    The row it landed on is the one at risk, and it is the one asked —
    a verdict measured against whichever slug was open when the worker
    started says nothing about the file now on screen.
    """
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        assert app.current_draft.slug == "230919-bear"
        await select_row(app, pilot, "○ 231002-crow")
        assert app.current_draft.slug == "231002-crow"

        crow = drafts / "231002-crow.md"
        crow.write_text(
            "title: crow\nslug: 231002-crow\n\nA crow from the admin.\n",
            encoding="utf-8",
        )
        await app._sync_arrived(
            SyncReport(updated=["231002-crow"]), "230919-bear", True
        )
        await pilot.pause()

        assert body_area(app).text == "A crow from the admin.\n"
        assert app.suppressed_changes == 0


async def test_a_row_changed_under_a_dirty_editor_is_marked_and_never_written_over(
    drafts,
):
    """The dirty half of the same case: neither version is thrown away.

    Nothing is reloaded over the unsaved text, and — the part that lost
    the server's work silently — the next parseable keystroke cannot
    save the older text back over what arrived.
    """
    app = WriterApp(client=StubClient(entries={"231002-crow": dict(CROW_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "231002-crow")

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # unsaved and unparseable
        await pilot.pause()

        crow = drafts / "231002-crow.md"
        server_text = "title: crow\nslug: 231002-crow\n\nA crow from the admin.\n"
        crow.write_text(server_text, encoding="utf-8")
        await app._sync_arrived(
            SyncReport(updated=["231002-crow"]), "230919-bear", True
        )
        await pilot.pause()

        assert header_area(app).text.startswith("xtitle: crow")
        assert body_area(app).text == "The crow body.\n"
        assert any(
            "231002-crow" in m and "older text" in m for m in notifications(app)
        )
        assert "⚠ 231002-crow" in labels(app)
        assert app._entry_state("231002-crow") == "conflict"
        on_disk = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert on_disk["231002-crow"]["state"] == "conflict"

        # Fixing the header would ordinarily save at once. Here what it
        # would save is the older body plus that fix, so it is refused.
        header_area(app).text = "title: crow\nslug: 231002-crow"
        await pilot.pause()
        assert crow.read_text(encoding="utf-8") == server_text
        assert "resolve" in status(app)

        # Reopening the row is the way out — through the conflict modal,
        # since Task 7 makes a `⚠` row open one instead of loading its
        # file straight away. Cancelling changes nothing and the file
        # still opens, which is what "editing works again" comes down to.
        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)
        await pilot.click("#cancel")
        await pilot.pause()
        assert body_area(app).text == "A crow from the admin.\n"
        assert status(app).startswith("✓")


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

        await select_row(app, pilot, "○ 230919-bear")
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


# --- conflict modal -------------------------------------------------------

async def test_conflict_row_opens_the_modal_and_cancel_changes_nothing(drafts):
    """Selecting a `⚠` row opens the modal instead of loading its file.

    Cancel is the no-op path: nothing in the sidecar or on disk moves,
    the row stays `⚠` — but the file still opens, since editing while
    conflicted is allowed and only publishing is blocked.
    """
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        assert app.current_draft.slug == "230919-bear"

        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)
        # Selecting the row alone must not have opened its file yet.
        assert app.current_draft.slug == "230919-bear"

        before = (drafts / ".sync.json").read_text(encoding="utf-8")
        await pilot.click("#cancel")
        await settle(app, pilot)

        assert not isinstance(app.screen, ConflictModal)
        assert (drafts / ".sync.json").read_text(encoding="utf-8") == before
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == CROW
        assert "⚠ 231002-crow" in labels(app)
        assert app.current_draft.slug == "231002-crow"
        assert body_area(app).text == "The crow body.\n"


async def test_conflict_keep_mine_advances_the_base_and_leaves_the_file_alone(drafts):
    """KEEP MINE on a state-only conflict has to create the base outright.

    The recorded entry starts as `{"state": "conflict"}` (plus whatever
    `assets_hash` the reconciliation pass attached) — no `hash`, no
    `date` — since this is a first-run conflict with nothing to compare
    against. Keep-mine must build the whole base from the server row,
    not just flip a label onto one that doesn't exist yet.
    """
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        before = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))["231002-crow"]
        assert before["state"] == "conflict"
        assert "hash" not in before
        assert "date" not in before

        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)

        await pilot.click("#keep-mine")
        await settle(app, pilot)

        assert not isinstance(app.screen, ConflictModal)
        after = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))["231002-crow"]
        assert after["hash"] == content_hash(
            "crow", "A crow that sings differently on the server.\n"
        )
        assert after["date"] == "2023-10-02"
        assert after["state"] == "edited"
        # The file is untouched — "mine" means the poet's text stands.
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == CROW
        assert "● 231002-crow" in labels(app)
        assert app.current_draft.slug == "231002-crow"
        assert body_area(app).text == "The crow body.\n"


async def test_conflict_take_server_rewrites_the_file_and_cleans_the_row(drafts):
    """TAKE SERVER: same write shape as the engine's own AUTO_UPDATE."""
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)

        await pilot.click("#take-server")
        await settle(app, pilot)

        assert not isinstance(app.screen, ConflictModal)
        expected = (
            "title: crow\nslug: 231002-crow\ndate: 2023-10-02\n\n"
            "A crow that sings differently on the server.\n"
        )
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == expected
        after = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))["231002-crow"]
        assert after["hash"] == content_hash(
            "crow", "A crow that sings differently on the server.\n"
        )
        assert after["date"] == "2023-10-02"
        assert after["state"] == "clean"
        assert "231002-crow" in labels(app)
        assert "⚠ 231002-crow" not in labels(app)
        assert "● 231002-crow" not in labels(app)
        assert app.current_draft.slug == "231002-crow"
        assert body_area(app).text == "A crow that sings differently on the server.\n"


async def test_conflict_view_diff_shows_a_server_only_line_and_back_returns(drafts):
    """VIEW DIFF replaces the choices with a unified diff; `back` restores them."""
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ConflictModal)

        await pilot.click("#view-diff")
        await settle_conflict_fetch(app, pilot)

        diff_text = str(modal.query_one("#diff-text", Static).content)
        assert "A crow that sings differently on the server." in diff_text
        assert "The crow body." in diff_text
        assert modal.query_one("#diff-pane").display is True
        assert modal.query_one("#choices").display is False

        await pilot.click("#back")
        await pilot.pause()
        assert modal.query_one("#diff-pane").display is False
        assert modal.query_one("#choices").display is True
        assert isinstance(app.screen, ConflictModal)

        # The diff is a detour, not a decision — cancel still works from here.
        await pilot.click("#cancel")
        await settle(app, pilot)
        assert "⚠ 231002-crow" in labels(app)
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == CROW


async def test_conflict_view_diff_survives_a_non_utf8_local_file(drafts):
    """An unreadable local file gets a diff, not a crash.

    A local `.md` that isn't valid UTF-8 is exactly the "content unknown"
    case `run_sync` already tolerates (`(DraftError, OSError,
    UnicodeDecodeError)`), which is how it lands as a `⚠` with no base in
    the first place. `_local_diff_halves` has to survive it too — it runs
    inside `_conflict_diff`, a thread worker, where an uncaught exception
    fails the worker and takes the whole app down with it.
    """
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "⚠ 231002-crow" in labels(app)

        # Written only now, after startup's own read of the file — crow
        # sorts first and would otherwise be auto-opened straight off
        # disk before any conflict is known, which answers to
        # `load_draft_into_editor`'s plain-OSError guard, not the one
        # under test here (`_local_diff_halves`, reached only through
        # the conflict modal below).
        (drafts / "231002-crow.md").write_bytes(b"\xff\xfe not valid utf-8 at all")

        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ConflictModal)

        await pilot.click("#view-diff")
        await settle_conflict_fetch(app, pilot)

        assert app.is_running
        diff_text = str(modal.query_one("#diff-text", Static).content)
        assert "A crow that sings differently on the server." in diff_text
        assert "cannot read 231002-crow.md" in diff_text


async def test_conflict_keep_mine_client_error_leaves_modal_open_and_sidecar_untouched(
    drafts,
):
    """A `ClientError` fetching the server row: a notify, and nothing else moves.

    The modal is left exactly as it was — live buttons, nothing written —
    so the poet can retry once the server answers again, or cancel.
    """
    client = StubClient(
        entries={"231002-crow": dict(CROW_SERVER)},
        fail_get_entry_for={"231002-crow"},
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        before = (drafts / ".sync.json").read_text(encoding="utf-8")

        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)

        await pilot.click("#keep-mine")
        await settle_conflict_fetch(app, pilot)

        assert isinstance(app.screen, ConflictModal)  # still open — retry or cancel
        assert (drafts / ".sync.json").read_text(encoding="utf-8") == before
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == CROW
        assert any("offline" in m for m in notifications(app))
        assert client.calls.get("get_entry") == 1


async def test_conflict_take_server_clears_the_stale_gate(drafts):
    """The autosave refusal from a file changed under the editor lifts too.

    `_file_changed_under_editor` marks a slug `⚠` and refuses autosave
    for it until the row is deliberately reopened. Resolving that same
    `⚠` through take-server has to be one of the ways that counts, not
    just a plain reopen with no modal in the way.
    """
    client = StubClient(entries={"231002-crow": dict(CROW_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "231002-crow")

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # unsaved and unparseable
        await pilot.pause()

        crow = drafts / "231002-crow.md"
        server_text = "title: crow\nslug: 231002-crow\n\nA crow from the admin.\n"
        crow.write_text(server_text, encoding="utf-8")
        client.entries["231002-crow"]["content_markdown"] = "A crow from the admin.\n"
        await app._sync_arrived(SyncReport(updated=["231002-crow"]), "230919-bear", True)
        await pilot.pause()

        assert app._stale_slug == "231002-crow"
        assert app._entry_state("231002-crow") == "conflict"

        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        assert isinstance(app.screen, ConflictModal)

        await pilot.click("#take-server")
        await settle(app, pilot)

        assert app._stale_slug is None
        assert crow.read_text(encoding="utf-8") == server_text
        assert body_area(app).text == "A crow from the admin.\n"

        # The refusal is really gone: a further edit autosaves normally.
        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert crow.read_text(encoding="utf-8") == (
            "title: crow\nslug: 231002-crow\n\n!A crow from the admin.\n"
        )
        assert status(app).startswith("✓")


# --- selection ----------------------------------------------------------

async def test_first_draft_is_loaded_on_start(drafts):
    """Newest-name-first: crow (231002) sorts ahead of bear (230919)."""
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert body_area(app).text == "The crow body.\n"
        assert app.current_draft.slug == "231002-crow"


async def test_selecting_a_draft_loads_its_body_and_header(drafts):
    """Crow opens first; "down" moves onto the second row, bear."""
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#sidebar", ListView).focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.current_draft.slug == "230919-bear"
        assert body_area(app).text == "The bear body.\n"
        assert header_area(app).text == (
            "title: bear\nslug: 230919-bear\ndate: 2023-09-19 08:00"
        )


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
        await select_row(app, pilot, "○ 230919-bear")
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


async def test_a_keystroke_landing_after_an_engine_rewrite_never_buries_it(drafts):
    """The autosave race, between the engine's write and the reload gate.

    `run_sync` rewrites the open file on its own thread; `_sync_arrived`
    only asks the editor to take that up afterwards. A keystroke landing
    in between autosaves what the editor still holds — text from before
    the rewrite — straight over the server's version. Both guards then
    measure clean (editor and file agree; the file is what the editor
    just wrote), so the buried server edit reads as an ordinary `●` and
    the next push finishes burying it.

    Neither guard can see it, because both compare the editor to *now*.
    The question this answers is a different one: is the file still what
    this app last read or wrote?
    """
    app = WriterApp(client=StubClient(entries={"230919-bear": dict(BEAR_ENTRY)}))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        path = drafts / "230919-bear.md"
        assert app._editor_is_clean()

        # The engine's write, with no `_sync_arrived` behind it yet.
        server_text = (
            "title: bear\nslug: 230919-bear\ndate: 2023-09-19 08:00\n"
            "\nA bear rewritten in the admin.\n"
        )
        path.write_text(server_text, encoding="utf-8")

        # ...and the keystroke that lands in the gap.
        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()

        assert path.read_text(encoding="utf-8") == server_text
        assert app._stale_slug == "230919-bear"
        assert "resolve" in status(app)
        assert app._entry_state("230919-bear") == "conflict"
        assert "⚠ 230919-bear" in labels(app)


async def test_an_ordinary_save_is_not_mistaken_for_a_file_that_moved(drafts):
    """The other half: the check must not fire on this app's own writes.

    A save canonicalizes the header, so what the file holds afterwards is
    not what the editors spell out — and comparing the two would refuse
    every keystroke after the first on a file written by hand.
    """
    path = drafts / "230919-bear.md"
    path.write_text(
        "title:   bear\nmood: blue\nslug: 230919-bear\n\nThe bear body.\n",
        encoding="utf-8",
    )
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert app._stale_slug is None
        assert status(app).startswith("✓")

    assert path.read_text(encoding="utf-8") == (
        "title: bear\nslug: 230919-bear\nmood: blue\n\n!?The bear body.\n"
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
        await select_row(app, pilot, "○ 230919-bear")
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
        await select_row(app, pilot, "○ 230919-bear")
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
        await select_row(app, pilot, "○ 230919-bear")
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
    """Doesn't care which draft is open — crow, the new first row, opens by default."""
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
        assert browser.calls == ["http://127.0.0.1:8000/draft-preview/231002-crow/"]
        assert starter.calls == 0
        assert app._server_process is None


async def test_preview_offers_to_start_the_server_when_it_is_down(drafts):
    """Doesn't care which draft is open — crow, the new first row, opens by default."""
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
        assert browser.calls == ["http://127.0.0.1:8000/draft-preview/231002-crow/"]


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
    """Doesn't care which draft is open — crow, the new first row, opens by default."""
    local_env(monkeypatch)
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, PublishModal)
        statement = str(app.screen.query_one("#statement", Static).content)
        assert statement == 'creates new entry "231002-crow"'


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
        await select_row(app, pilot, "○ 230919-bear")
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
        await select_row(app, pilot, "○ 230919-bear")
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
        await select_row(app, pilot, "○ 230919-bear")
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

        await select_row(app, pilot, "○ 230919-bear")
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


async def test_publish_refuses_a_conflicted_slug_before_any_client_call(drafts, monkeypatch):
    """Failure rule 8: a `⚠` slug is refused before the modal even opens.

    The open draft is put into conflict via the same first-run path the
    conflict-modal tests use, then cancelled out of the modal — cancel
    leaves the row `⚠` and still opens the file, which is exactly the
    "editing while conflicted is allowed" case this refusal exists for.
    """
    local_env(monkeypatch)
    client = StubClient(entries={"231002-crow": dict(CROW_SERVER)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await select_row(app, pilot, "⚠ 231002-crow")
        await pilot.pause()
        await pilot.click("#cancel")
        await settle(app, pilot)
        assert app.current_draft.slug == "231002-crow"
        assert app._entry_state("231002-crow") == "conflict"

        calls_before = dict(client.calls)
        publish_calls_before = list(client.publish_calls)
        await pilot.press("ctrl+b")
        await pilot.pause()

        assert not isinstance(app.screen, PublishModal)
        assert status(app) == "resolve the conflict on 231002-crow first"
        assert client.calls == calls_before
        assert client.publish_calls == publish_calls_before


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


# --- publish base-advance -----------------------------------------------

async def test_first_publish_of_a_draft_creates_its_base_and_moves_to_the_log(
    drafts, monkeypatch
):
    """The sidecar itself, not just the label — the row moves sections too."""
    local_env(monkeypatch)
    client = StubClient()  # entries={} — 230919-bear starts local-only
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "○ 230919-bear" in labels(app)
        assert app._entry_state("230919-bear") == "local-only"

        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert "230919-bear" in labels(app)
        assert "○ 230919-bear" not in labels(app)
        assert app._entry_state("230919-bear") == "clean"

        state = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert state["230919-bear"]["state"] == "clean"
        assert state["230919-bear"]["hash"] == content_hash("bear", "The bear body.\n")
        assert state["230919-bear"]["date"] == "2023-09-19 08:00"


class DateNormalizingClient(StubClient):
    """Models a server that reformats the date it's handed.

    A publish whose base-advance blindly trusted the date it just sent
    would read this as the local file having moved out from under the
    base the instant it was recorded — a spurious conflict on the very
    next sync. This exists to prove the base-advance instead reads the
    date back from `get_entry` and canonicalizes the local header to it.
    """

    def upsert_entry(self, slug, title, content_markdown, publish_date=None,
                      share_bluesky=False, share_mastodon=False):
        normalized = f"{publish_date}:00" if publish_date else publish_date
        return super().upsert_entry(
            slug, title, content_markdown, publish_date=normalized,
            share_bluesky=share_bluesky, share_mastodon=share_mastodon,
        )


async def test_publish_canonicalizes_the_date_header_without_a_stale_gate_misfire(
    drafts, monkeypatch
):
    local_env(monkeypatch)
    client = DateNormalizingClient()  # entries={} — create case
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        assert app.current_draft.slug == "230919-bear"

        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        # The server's reformatted date landed both on disk and in the
        # editor that was open on it — not just one or the other.
        on_disk = (drafts / "230919-bear.md").read_text(encoding="utf-8")
        assert "date: 2023-09-19 08:00:00" in on_disk
        assert "date: 2023-09-19 08:00:00" in header_area(app).text

        # The base agrees with what actually landed, and the row is clean.
        state = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert state["230919-bear"]["date"] == "2023-09-19 08:00:00"
        assert state["230919-bear"]["state"] == "clean"
        assert app._entry_state("230919-bear") == "clean"

        # No stale-gate misfire: no ✗, no stray conflict, and the editor's
        # own baseline really is in step with the file — proven by a
        # further keystroke autosaving normally rather than tripping the
        # "file changed under the editor" refusal.
        assert app._stale_slug is None
        assert not status(app).startswith("✗")
        assert app.suppressed_changes == 0

        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert app._entry_state("230919-bear") == "edited"
        assert not status(app).startswith("✗")
        assert "resolve" not in status(app)


async def test_publish_with_a_dirty_editor_defers_the_canonicalization_quietly(
    drafts, monkeypatch
):
    """A rewrite this app *declined* to make must not be reported as one.

    The canonicalizing write is skipped while the open editor holds
    unsaved text — but the file was then never touched, so calling
    `_file_changed_under_editor` on it invents a change: a ⚠ written over
    the base that just advanced, a notification claiming the disk moved,
    autosave refused, and advice to "reopen the row" that would throw the
    on-screen text away. The deferral itself is all that was wanted, and
    the next sync's guarded ADVANCE_BASE already performs it.
    """
    local_env(monkeypatch)
    client = DateNormalizingClient()  # entries={} — create case
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        assert app.current_draft.slug == "230919-bear"

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # "xtitle: bear" — unsaved and unparseable
        await pilot.pause()
        assert status(app).startswith("✗")

        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        # The publish landed and the base advanced to the server's row.
        state = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert state["230919-bear"]["date"] == "2023-09-19 08:00:00"
        assert state["230919-bear"]["hash"] == content_hash("bear", "The bear body.\n")

        # Nothing wrote the file, so nothing may claim it moved.
        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == BEAR
        assert app._stale_slug is None
        assert app._entry_state("230919-bear") != "conflict"
        assert "⚠ 230919-bear" not in labels(app)
        assert not any("changed on disk" in m for m in notifications(app))

        # The unsaved text is still on screen, and still the poet's to fix.
        assert header_area(app).text.startswith("xtitle: bear")
        assert body_area(app).text == "The bear body.\n"

        # And fixing it saves, rather than meeting a refusal it never earned.
        header_area(app).text = "title: bear\nslug: 230919-bear\ndate: 2023-09-19 08:00"
        await pilot.pause()
        assert not status(app).startswith("✗")
        assert "resolve" not in status(app)
        assert (drafts / "230919-bear.md").read_text(encoding="utf-8") == BEAR


async def test_a_sync_that_clears_the_conflict_lifts_the_stale_gate_too(drafts):
    """The gate's lifetime is the ⚠'s, not the session's.

    `_file_changed_under_editor` marks the row ⚠ and refuses autosave for
    it. When a later sync re-baselines that slug and the ⚠ goes away, a
    refusal that outlived it is a file nothing in the app will ever write
    to again — with no ⚠ left on screen to explain why.
    """
    client = StubClient(entries={"231002-crow": dict(CROW_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "231002-crow")

        area = header_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("x")  # unsaved and unparseable
        await pilot.pause()

        crow = drafts / "231002-crow.md"
        server_text = "title: crow\nslug: 231002-crow\n\nA crow from the admin.\n"
        crow.write_text(server_text, encoding="utf-8")
        client.entries["231002-crow"]["content_markdown"] = "A crow from the admin.\n"
        await app._sync_arrived(SyncReport(updated=["231002-crow"]), "230919-bear", True)
        await pilot.pause()
        assert app._stale_slug == "231002-crow"
        assert app._entry_state("231002-crow") == "conflict"

        # Resolve the ⚠ the way a sync would: the row is re-baselined
        # against the server and reads clean again.
        app._advance_conflict_base(
            "231002-crow",
            hash=content_hash("crow", "A crow from the admin.\n"),
            date=None,
            state="clean",
        )
        await pilot.pause()
        assert app._entry_state("231002-crow") == "clean"
        assert app._stale_slug is None

        # The gate lifted, but the editor still holds the older text over a
        # file that really did move — so the next keystroke re-earns it
        # rather than burying what arrived.
        area = body_area(app)
        area.focus()
        area.move_cursor((0, 0))
        await pilot.press("!")
        await pilot.pause()
        assert crow.read_text(encoding="utf-8") == server_text
        assert app._stale_slug == "231002-crow"


async def test_update_publish_advances_base_from_the_servers_own_row(drafts, monkeypatch):
    """The recorded hash comes from the server's row, not the local draft —
    proven with a server that reformats the date, so a naive base built
    from what was *sent* would disagree with a fresh sync's own read of
    the server and mark the row a conflict instead of clean.
    """
    local_env(monkeypatch)
    client = DateNormalizingClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "230919-bear")
        await pilot.press("ctrl+b")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await settle(app, pilot)

        assert app._entry_state("230919-bear") == "clean"
        state = json.loads((drafts / ".sync.json").read_text(encoding="utf-8"))
        assert state["230919-bear"]["hash"] == content_hash("bear", "The bear body.\n")
        assert state["230919-bear"]["date"] == "2023-09-19 08:00:00"

        # A further sync (ctrl+r) must not find anything to reconcile —
        # if the base had been built from what was sent instead of the
        # server's own row, this sync would find the server "changed"
        # (the reformatted date) against a base that never recorded it.
        calls_before = client.calls["list_entries_full"]
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert client.calls["list_entries_full"] == calls_before + 1
        assert app._entry_state("230919-bear") == "clean"
        assert any(m == "synced" for m in notifications(app))


# --- push-all -------------------------------------------------------------

async def _edit_open_row(pilot, app, slug: str) -> None:
    """Select `slug`'s row and type one character at the body's start.

    The idiomatic way this suite already turns a mirrored, clean row
    into `"edited"` — see `test_an_edited_mirrored_entry_shows_its_marker
    _at_once` — reused here so push-all has real `●` rows, each with a
    real base behind it, to work with.
    """
    await select_row(app, pilot, slug)
    area = body_area(app)
    area.focus()
    area.move_cursor((0, 0))
    await pilot.press("!")
    await pilot.pause()


async def test_push_all_pushes_edited_rows_assets_before_entry_in_sidebar_order(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    local_env(monkeypatch)
    client = StubClient(entries={
        "230919-bear": dict(BEAR_ENTRY),
        "231002-crow": dict(CROW_ENTRY),
    })
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)  # first sync adopts both, clean

        for slug in ("231002-crow", "230919-bear"):  # deliberately out of sidebar order
            await _edit_open_row(pilot, app, slug)
            assert app._entry_state(slug) == "edited"

        state_before = json.loads((tmp_path / ".sync.json").read_text(encoding="utf-8"))
        assets_hash_before = {
            slug: state_before[slug].get("assets_hash")
            for slug in ("230919-bear", "231002-crow")
        }
        for slug in ("230919-bear", "231002-crow"):
            assets = tmp_path / f"{slug}.assets"
            assets.mkdir()
            (assets / f"{slug}.png").write_bytes(slug.encode())

        # bear sorts before crow in the log section (it has a date, crow
        # doesn't) — sidebar order, not edit order, is what push-all uses.
        assert labels(app).index("● 230919-bear") < labels(app).index("● 231002-crow")

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        assert client.publish_calls == [
            ("upload", "230919-bear.png"),
            (
                "upsert", "230919-bear", "bear", "!The bear body.\n",
                "2023-09-19 08:00", False, False,
            ),
            ("upload", "231002-crow.png"),
            ("upsert", "231002-crow", "crow", "!The crow body.\n", None, False, False),
        ]
        assert client.calls.get("get_entry", 0) == 2

        assert app._entry_state("230919-bear") == "clean"
        assert app._entry_state("231002-crow") == "clean"
        state = json.loads((tmp_path / ".sync.json").read_text(encoding="utf-8"))
        assert state["230919-bear"]["hash"] == content_hash("bear", "!The bear body.\n")
        assert state["231002-crow"]["hash"] == content_hash("crow", "!The crow body.\n")
        # assets_hash is reconciliation's alone — push-all never touches it
        assert state["230919-bear"].get("assets_hash") == assets_hash_before["230919-bear"]
        assert state["231002-crow"].get("assets_hash") == assets_hash_before["231002-crow"]

        assert any(m == "pushed 2" for m in notifications(app))


async def test_push_all_skips_conflict_rows_and_names_them_in_the_summary(
    drafts, monkeypatch
):
    local_env(monkeypatch)
    client = StubClient(entries={
        "230919-bear": dict(BEAR_ENTRY),
        "231002-crow": dict(CROW_SERVER),  # first-run conflict, no shared base
    })
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app._entry_state("231002-crow") == "conflict"

        await _edit_open_row(pilot, app, "230919-bear")
        assert app._entry_state("230919-bear") == "edited"

        client.publish_calls = []
        await pilot.press("ctrl+f")
        await settle(app, pilot)

        pushed_slugs = {c[1] for c in client.publish_calls if c[0] == "upsert"}
        assert pushed_slugs == {"230919-bear"}
        assert app._entry_state("230919-bear") == "clean"
        assert app._entry_state("231002-crow") == "conflict"  # untouched, not overwritten

        assert any(
            m == "pushed 1 · 1 conflict skipped (231002-crow)" for m in notifications(app)
        )


async def test_push_all_excludes_local_only_drafts(drafts, monkeypatch):
    local_env(monkeypatch)
    client = StubClient()  # entries={} — both drafts are local-only, nothing mirrored
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app._entry_state("230919-bear") == "local-only"
        assert app._entry_state("231002-crow") == "local-only"

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        assert client.publish_calls == []
        assert any(m == "nothing to push" for m in notifications(app))


class FlakyOnceClient(StubClient):
    """Fails `upsert_entry` for one target slug on its first call, then recovers.

    Models a server hiccup that clears itself — the shape push-all's
    "any `ClientError` stops the run, a re-run converges" rule is
    written for. Every other call, and every later call for the same
    slug, behaves exactly like `StubClient`.
    """

    def __init__(self, *args, fail_upsert_once_for=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_upsert_once_for = fail_upsert_once_for

    def upsert_entry(self, slug, *args, **kwargs):
        if slug == self._fail_upsert_once_for:
            self._fail_upsert_once_for = None
            raise ClientError("500 server hiccup")
        return super().upsert_entry(slug, *args, **kwargs)


async def test_push_all_mid_run_client_error_stops_and_a_rerun_converges(
    drafts, monkeypatch
):
    local_env(monkeypatch)
    client = FlakyOnceClient(
        entries={
            "230919-bear": dict(BEAR_ENTRY),
            "231002-crow": dict(CROW_ENTRY),
        },
        fail_upsert_once_for="231002-crow",  # bear sorts first, crow second
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        for slug in ("230919-bear", "231002-crow"):
            await _edit_open_row(pilot, app, slug)
            assert app._entry_state(slug) == "edited"

        await pilot.press("ctrl+f")
        await settle(app, pilot)

        # bear (first, before the failure) landed and went clean; crow
        # (where the ClientError hit) is left exactly as it was — ●.
        assert app._entry_state("230919-bear") == "clean"
        assert app._entry_state("231002-crow") == "edited"
        assert any(
            "500 server hiccup" in m and "230919-bear" in m for m in notifications(app)
        )
        # crow's upsert raised before `StubClient.upsert_entry` ever ran,
        # so it never landed a call at all — not even a failed one.
        assert not any(c[0] == "upsert" and c[1] == "231002-crow" for c in client.publish_calls)

        # Re-running converges: crow's asset (none here) and upsert are
        # retried from scratch and this time land, since the stub's
        # failure was one-shot.
        await pilot.press("ctrl+f")
        await settle(app, pilot)

        assert app._entry_state("231002-crow") == "clean"
        assert any(m == "pushed 1" for m in notifications(app))


class SidecarFlippingClient(StubClient):
    """Turns another slug's sidecar row `⚠` while one slug is being upserted.

    Stands in for the sync worker that finishes its discovery while
    push-all is walking a snapshot taken before it started: by the time
    the second slug's turn comes, a row push-all remembers as `●` is `⚠`
    on disk, and pushing it buries exactly the server edit rule 8 exists
    to protect.
    """

    def __init__(self, *args, flip_on=None, flip_slug=None, state_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._flip_on = flip_on
        self._flip_slug = flip_slug
        self._state_path = state_path

    def upsert_entry(self, slug, *args, **kwargs):
        if slug == self._flip_on:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            state[self._flip_slug]["state"] = "conflict"
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        return super().upsert_entry(slug, *args, **kwargs)


async def test_push_all_rechecks_each_slug_and_skips_one_that_turned_conflict(
    drafts, monkeypatch
):
    """The snapshot is a starting point, not a warrant — every slug is re-read."""
    local_env(monkeypatch)
    client = SidecarFlippingClient(
        entries={
            "230919-bear": dict(BEAR_ENTRY),
            "231002-crow": dict(CROW_ENTRY),
        },
        flip_on="230919-bear",    # bear sorts first and pushes first...
        flip_slug="231002-crow",  # ...and crow turns ⚠ while it does
        state_path=drafts / ".sync.json",
    )
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        for slug in ("230919-bear", "231002-crow"):
            await _edit_open_row(pilot, app, slug)
            assert app._entry_state(slug) == "edited"

        client.publish_calls = []
        await pilot.press("ctrl+f")
        await settle(app, pilot)

        pushed = {c[1] for c in client.publish_calls if c[0] == "upsert"}
        assert pushed == {"230919-bear"}
        assert app._entry_state("231002-crow") == "conflict"
        # The local file is untouched, and the skip is named rather than silent.
        assert (drafts / "231002-crow.md").read_text(encoding="utf-8") == (
            "title: crow\nslug: 231002-crow\n\n!The crow body.\n"
        )
        assert any(
            m == "pushed 1 · 1 conflict skipped (231002-crow)" for m in notifications(app)
        )


async def test_push_all_and_publish_are_refused_while_a_sync_is_running(
    drafts, monkeypatch
):
    """A sync mid-discovery outranks a push, for the same reason a ⚠ does.

    The `●` rows push-all snapshots are what the *last* sync knew. A sync
    still in flight may be on its way to calling one of them `⚠`, and a
    push that got there first would bury the server edit that made it one.
    """
    local_env(monkeypatch)
    release = threading.Event()

    class SlowSyncClient(StubClient):
        block = False

        def list_entries_full(self):
            if self.block:
                release.wait(timeout=5)
            return super().list_entries_full()

    client = SlowSyncClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await _edit_open_row(pilot, app, "230919-bear")
        assert app._entry_state("230919-bear") == "edited"

        client.block = True
        client.publish_calls = []
        await pilot.press("ctrl+r")
        for _ in range(200):
            if any(w.group == "sync" and w.is_running for w in app.workers):
                break
            await pilot.pause()
        else:
            raise AssertionError("sync worker never started running")

        await pilot.press("ctrl+f")
        await pilot.pause()
        assert status(app) == "a sync is already running"

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert status(app) == "a sync is already running"
        assert not isinstance(app.screen, PublishModal)

        assert client.publish_calls == []

        client.block = False
        release.set()
        await settle(app, pilot)


async def test_push_all_refused_while_a_publish_worker_is_running(drafts, monkeypatch):
    """One `"publish"`-group worker at a time — no silent overlap."""
    local_env(monkeypatch)
    release = threading.Event()

    class BlockingClient(StubClient):
        def upsert_entry(self, *args, **kwargs):
            release.wait(timeout=5)
            return super().upsert_entry(*args, **kwargs)

    client = BlockingClient(entries={"230919-bear": dict(BEAR_ENTRY)})
    app = WriterApp(client=client)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await _edit_open_row(pilot, app, "230919-bear")
        assert app._entry_state("230919-bear") == "edited"

        await pilot.press("ctrl+f")
        for _ in range(200):
            workers = [w for w in app.workers if w.group == "publish"]
            if workers and workers[0].is_running:
                break
            await pilot.pause()
        else:
            raise AssertionError("push-all worker never started running")

        calls_before = list(client.publish_calls)

        # A second push-all, while the first is still blocked mid-flight,
        # is refused outright rather than cancelling the first.
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert status(app) == "a publish is already running"

        # And so is a single-entry publish — the two share the group.
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert status(app) == "a push is already running"
        assert not isinstance(app.screen, PublishModal)

        assert client.publish_calls == calls_before

        release.set()
        await settle(app, pilot)
        assert app._entry_state("230919-bear") == "clean"


# --- add image --------------------------------------------------------------


async def test_add_image_copies_the_file_and_inserts_the_reference(drafts, tmp_path):
    source = tmp_path / "outside" / "dusk-road.jpg"
    source.parent.mkdir()
    source.write_bytes(b"synthetic-image-bytes")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert isinstance(app.screen, AddImageModal)

        app.screen.query_one("#path", Input).value = str(source)
        await pilot.click("#confirm")
        await pilot.pause()

        copied = drafts / "230919-bear.assets" / "dusk-road.jpg"
        assert copied.read_bytes() == b"synthetic-image-bytes"
        body = app.query_one("#body", TextArea)
        assert "![](/media/log_assets/dusk-road.jpg)" in body.text
        # The cursor sits inside the alt brackets, ready for alt text.
        row, col = body.cursor_location
        line = body.text.split("\n")[row]
        assert line[col - 2:col] == "!["
        # Autosave carried the reference to disk.
        assert "![](/media/log_assets/dusk-road.jpg)" in (
            (drafts / "230919-bear.md").read_text(encoding="utf-8"))
        assert not isinstance(app.screen, AddImageModal)


async def test_add_image_custom_name_wins_over_the_default(drafts, tmp_path):
    source = tmp_path / "IMG_4821.jpg"
    source.write_bytes(b"synthetic-image-bytes")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(source)
        app.screen.query_one("#name", Input).value = "230919-bear-walk.jpg"
        await pilot.click("#confirm")
        await pilot.pause()

        assert (drafts / "230919-bear.assets" / "230919-bear-walk.jpg").exists()
        assert "![](/media/log_assets/230919-bear-walk.jpg)" in (
            app.query_one("#body", TextArea).text)


async def test_add_image_refuses_a_missing_source_and_keeps_the_modal_open(drafts):
    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = "/nonexistent/pig.jpg"
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, AddImageModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert not (drafts / "230919-bear.assets" / "pig.jpg").exists()


async def test_add_image_refuses_a_name_collision_with_different_content(drafts, tmp_path):
    source = tmp_path / "dusk-road.jpg"
    source.write_bytes(b"new-bytes")
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "dusk-road.jpg").write_bytes(b"old-bytes")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(source)
        await pilot.click("#confirm")
        await pilot.pause()

        assert isinstance(app.screen, AddImageModal)
        assert str(app.screen.query_one("#error", Static).content)
        assert (assets / "dusk-road.jpg").read_bytes() == b"old-bytes"


async def test_add_image_same_content_is_a_quiet_no_op_that_still_inserts(drafts, tmp_path):
    source = tmp_path / "dusk-road.jpg"
    source.write_bytes(b"same-bytes")
    assets = drafts / "230919-bear.assets"
    assets.mkdir()
    (assets / "dusk-road.jpg").write_bytes(b"same-bytes")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_row(app, pilot, "○ 230919-bear")
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(source)
        await pilot.click("#confirm")
        await pilot.pause()

        assert not isinstance(app.screen, AddImageModal)
        assert "![](/media/log_assets/dusk-road.jpg)" in (
            app.query_one("#body", TextArea).text)


async def test_add_image_cancel_does_nothing(drafts, tmp_path):
    source = tmp_path / "dusk-road.jpg"
    source.write_bytes(b"synthetic-image-bytes")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        body_before = app.query_one("#body", TextArea).text
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(source)
        await pilot.click("#cancel")
        await pilot.pause()

        assert not (drafts / "230919-bear.assets" / "dusk-road.jpg").exists()
        assert app.query_one("#body", TextArea).text == body_before


async def test_drafts_section_lists_newest_name_first(drafts):
    (drafts / "220101-old-draft.md").write_text(
        "title: old\nslug: 220101-old-draft\n\nbody.\n", encoding="utf-8")
    (drafts / "231103-fox.md").write_text(
        "title: fox\nslug: 231103-fox\n\nbody.\n", encoding="utf-8")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        rows = labels(app)
        assert rows.index("○ 231103-fox") < rows.index("○ 220101-old-draft")


async def test_a_non_utf8_first_row_does_not_crash_startup(drafts):
    # 240101-junk sorts newest, so it is the startup auto-open row.
    (drafts / "240101-junk.md").write_bytes(b"\xff\xfe not utf-8 \xff")

    app = WriterApp(client=StubClient())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.is_running
        assert "cannot read 240101-junk.md" in status(app)
        # The rest of the app still works: another row opens normally.
        await select_row(app, pilot, "○ 231002-crow")
        assert app.current_draft is not None
        assert app.current_draft.slug == "231002-crow"
