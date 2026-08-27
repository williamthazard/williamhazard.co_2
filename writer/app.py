"""The terminal app: a list of drafts, a two-tab editor, a status line.

The layout is a sidebar of local-only draft files on the left (below a
divider, the log — every entry the server has, mirrored to disk), an
editor on the right with a `draft` tab for the body and a `meta` tab for
the `key: value` header, and one status line along the bottom.

The draft file on disk is the source of truth. Every keystroke in either
editor re-parses the whole draft; a parse failure shows on the status line
and nothing is written, and a parse success is saved immediately. There is
no unsaved state to lose and no save key to remember (`ctrl+s` exists, but
only forces what autosave has already done).

The meta pane holds the file's own header bytes, not a re-serialization of
what parsed out of them, so a key this format does not know is visible
before it can be lost — and `serialize_draft` carries such keys, so it
isn't lost. The gauge reports the last parse outcome, not the last save:
while the editors hold text that does not parse it reads `✗`, whatever
else redraws it.

Filling an editor programmatically also raises `TextArea.Changed`, which
would look exactly like typing. Each such assignment increments
`_suppress_changes`, and the handler spends one increment per event before
it will save anything, so a load can never write a file back.

The log half of the sidebar is a mirror, not a fetch cache. `writer/
sync.py` writes every published entry into the same drafts directory and
records what it saw in `.sync.json`; the sidebar reads that sidecar and
marks each row — `●` edited here since the last sync, `⚠` moved on both
sides, unmarked when the two agree. Every row, either section, opens a
local file: there is nothing in this app that only exists on the server.

Sync runs in a thread worker at startup and on `ctrl+r`. A server that
cannot be reached — or that will not answer without a token — is an
ordinary condition, not an error: the section reads `(offline)`, the
markers go on saying whatever the last sync knew, and every file stays
editable. Worker threads never touch a widget; results come back through
`call_from_thread`.

The file being edited is never rewritten out from under it: the worker is
handed the open slug and whether its editor is clean before it starts,
and an update that would land on a dirty editor is marked a conflict
instead of applied. When the sync comes back, what to take up is asked
of the editor that is open *then*, against its own baseline — a row
switched into while the worker ran was never covered by that guard, and
reloading it over unsaved text, or letting that text save back over what
just arrived, would both lose work quietly.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import webbrowser
from pathlib import Path

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from writer.client import ClientError, WriterClient
from writer.draft import (
    Draft,
    DraftError,
    assets_dir,
    drafts_dir,
    list_drafts,
    parse_draft,
    save_draft,
    serialize_draft,
)
from writer.sync import SyncReport, content_hash, load_state, run_sync, save_state

# The in-development site's deploy; williamhazard.co still serves the old
# site and must not be targeted until it aliases to this deploy.
DEFAULT_BASE_URL = "https://williamhazard-web.onrender.com"

# The repo root — parent of this `writer/` package — is where `manage.py`
# and the server's own `.venv/` (not this package's `writer/.venv/`) live.
REPO_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_HOST = "http://127.0.0.1:8000"

_DIVIDER = "── log ──"
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

# What each sync state puts in front of a log row's slug. `clean` is
# deliberately empty: an entry that agrees with the server is the quiet
# case, and only the two that ask something of the poet are marked.
_MARKERS = {"clean": "", "edited": "● ", "conflict": "⚠ "}
_LOCAL_ONLY = "○ "

# Shown, and kept showing, while the open file holds something the
# editor never saw — see `_file_changed_under_editor`.
_STALE_MESSAGE = "file changed under the editor — resolve ⚠ before editing"


def split_source(text: str) -> tuple[str, str]:
    """A draft file's header bytes and body bytes, at the first blank line.

    Deliberately not `parse_draft` plus `serialize_draft`: the meta pane
    must show what the file actually says — unknown keys, their order,
    their spacing — so that nothing can be lost before it is seen. Text
    with no blank line at all is all header, which is what `parse_draft`
    will (rightly) refuse.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "":
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return text, ""


def default_client() -> WriterClient:
    """A client for the configured server.

    The base URL comes from `BLOG_WRITER_BASE_URL` and the token, if any,
    from `BLOG_WRITER_TOKEN`. Without a token the client still works
    against a local DEBUG server; against production the entries list will
    refuse, which is simply the offline path.
    """
    return WriterClient(
        os.environ.get("BLOG_WRITER_BASE_URL", DEFAULT_BASE_URL),
        token=os.environ.get("BLOG_WRITER_TOKEN") or None,
    )


def _state_path() -> Path:
    """The sync sidecar, beside the drafts it describes."""
    return drafts_dir() / ".sync.json"


def _sync_line(report: SyncReport) -> str:
    """One line for what a sync did: `synced · 2 new · 1 conflict`.

    Zero parts are left out entirely, so a sync that found nothing to do
    says `synced` and nothing else — the common case, and the one that
    should take the least reading.
    """
    parts = ["synced"]
    if report.new:
        parts.append(f"{len(report.new)} new")
    if report.updated:
        parts.append(f"{len(report.updated)} updated")
    if report.conflicts:
        count = len(report.conflicts)
        parts.append(f"{count} conflict" if count == 1 else f"{count} conflicts")
    return " · ".join(parts)


def _error_line(errors: list[str]) -> str:
    """The first thing that went wrong, and how many others did too.

    Asset failures don't stop a sync, but they must not vanish either:
    an image that never landed is a page that renders wrong, and the
    poet is the only one who can decide what to do about it.
    """
    if len(errors) == 1:
        return errors[0]
    return f"{errors[0]} (+{len(errors) - 1} more)"


def _is_local(base_url: str) -> bool:
    """Is this the local DEBUG server, which allows publishing without a token?

    Anything else — production included — requires `BLOG_WRITER_TOKEN`.
    """
    return base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")


class SidebarItem(ListItem):
    """One row of the sidebar.

    `kind` is what the row stands for — `draft` (a local-only file the
    server has never seen), `log` (a mirrored entry), or one of the
    non-selectable `divider` / `note` rows. Both selectable kinds carry
    the path of a local file and the slug it goes by; the difference is
    which section they sit in and which marker they wear, not whether
    they can be opened.
    """

    def __init__(
        self,
        label: str,
        *,
        kind: str,
        draft_path: Path | None = None,
        slug: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(Static(label), classes=f"row-{kind}", disabled=disabled)
        self.label = label
        self.kind = kind
        self.draft_path = draft_path
        self.slug = slug


def _default_probe() -> bool:
    """Is something already answering on the preview port?

    Any response at all — even an error status — means a server is there;
    only a transport-level failure (connection refused, no route) means it
    isn't. A short timeout keeps a down server from stalling the app.
    """
    try:
        httpx.get(f"{PREVIEW_HOST}/", timeout=0.5)
    except httpx.TransportError:
        return False
    return True


def _default_start_server() -> subprocess.Popen:
    """Launch the local dev server from the repo root."""
    return subprocess.Popen(
        [".venv/bin/python", "manage.py", "runserver"], cwd=str(REPO_ROOT)
    )


class NewDraftModal(ModalScreen[bool]):
    """title / slug / date, validated before the file is created.

    `attempt` is a callable of `(title, slug, date) -> str | None` that
    performs the validation and, on success, the actual file creation —
    returning an error message keeps this modal open with that reason
    shown; returning `None` dismisses it with `True`.
    """

    DEFAULT_CSS = """
    NewDraftModal {
        align: center middle;
    }
    NewDraftModal > #dialog {
        width: 50;
        height: auto;
        border: solid $panel-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    NewDraftModal Input {
        margin-bottom: 1;
    }
    NewDraftModal #error {
        color: $error;
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, attempt) -> None:
        super().__init__()
        self._attempt = attempt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("new draft")
            yield Input(placeholder="title", id="title")
            yield Input(placeholder="slug", id="slug")
            yield Input(placeholder="date (optional)", id="date")
            yield Static("", id="error")
            with Horizontal():
                yield Button("create", id="confirm", variant="primary")
                yield Button("cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        title = self.query_one("#title", Input).value.strip()
        slug = self.query_one("#slug", Input).value.strip()
        date = self.query_one("#date", Input).value.strip() or None
        error = self._attempt(title, slug, date)
        if error:
            self.query_one("#error", Static).update(error)
            return
        self.dismiss(True)


class StartServerModal(ModalScreen[bool]):
    """Offers to start the local dev server when preview finds it down."""

    DEFAULT_CSS = """
    StartServerModal {
        align: center middle;
    }
    StartServerModal > #dialog {
        width: 50;
        height: auto;
        border: solid $panel-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("the dev server isn't running — start it?")
            with Horizontal():
                yield Button("start", id="confirm", variant="primary")
                yield Button("cancel", id="cancel")

    @on(Button.Pressed, "#confirm")
    def _start(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


class PublishModal(ModalScreen[bool]):
    """The share checkboxes and confirmation for publishing one draft.

    `attempt` is a callable of `(share_bluesky, share_mastodon) -> str |
    None`, mirroring `NewDraftModal`'s pattern: it runs the fast, local
    precondition check (the token check — no network) and, if it passes,
    launches the actual publish work in the background before returning
    `None`, which dismisses this modal immediately. It does not wait for
    that background work to finish — uploads and the entry upsert are
    network calls and must never block the UI thread, so any failure from
    them surfaces later as a notification, not in this modal. Returning a
    message instead keeps the modal open with that reason shown, exactly
    like a failed new-draft attempt.
    """

    DEFAULT_CSS = """
    PublishModal {
        align: center middle;
    }
    PublishModal > #dialog {
        width: 50;
        height: auto;
        border: solid $panel-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    PublishModal #statement {
        margin-bottom: 1;
    }
    PublishModal Checkbox {
        margin-bottom: 1;
    }
    PublishModal #error {
        color: $error;
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, statement: str, attempt) -> None:
        super().__init__()
        self._statement = statement
        self._attempt = attempt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._statement, id="statement")
            yield Checkbox("share to bluesky", value=False, id="bluesky")
            yield Checkbox("share to mastodon", value=False, id="mastodon")
            yield Static("", id="error")
            with Horizontal():
                yield Button("publish", id="confirm", variant="primary")
                yield Button("cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        share_bluesky = self.query_one("#bluesky", Checkbox).value
        share_mastodon = self.query_one("#mastodon", Checkbox).value
        error = self._attempt(share_bluesky, share_mastodon)
        if error:
            self.query_one("#error", Static).update(error)
            return
        self.dismiss(True)


class ConflictModal(ModalScreen[None]):
    """keep mine / take server / view diff / cancel for one `⚠` slug.

    Opened in place of loading the file directly (see `WriterApp._take_up`)
    — a conflict is a real fork the poet has to choose about, not
    something a row-select should paper over. `keep_mine` and
    `take_server` are `() -> None`: each starts real work off the UI
    thread (a `get_entry` fetch, then a write) and returns at once. This
    screen does not know when that work finishes — it stays exactly as
    it is, buttons live, until the app itself calls `dismiss()` on
    success or leaves it alone (a `ClientError` only reaches the poet as
    a notification) so a retry or a cancel is always available. `diff` is
    the same shape, calling `show_diff` instead of dismissing.

    Cancel needs nothing from the app: it changes nothing and simply
    closes, the same way `PublishModal`'s cancel does.
    """

    DEFAULT_CSS = """
    ConflictModal {
        align: center middle;
    }
    ConflictModal > #dialog {
        width: 90%;
        height: auto;
        max-height: 90%;
        border: solid $panel-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    ConflictModal #statement {
        margin-bottom: 1;
    }
    ConflictModal #diff-pane {
        height: 12;
        border: solid $panel-lighten-2;
        margin-bottom: 1;
    }
    ConflictModal #choices Button,
    ConflictModal #back-row Button {
        margin-right: 1;
    }
    """

    def __init__(self, slug: str, keep_mine, take_server, diff) -> None:
        super().__init__()
        self._slug = slug
        self._keep_mine = keep_mine
        self._take_server = take_server
        self._diff = diff

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"⚠ {self._slug} has moved on both sides", id="statement")
            with VerticalScroll(id="diff-pane"):
                yield Static("", id="diff-text")
            with Horizontal(id="choices"):
                yield Button("keep mine", id="keep-mine", variant="primary")
                yield Button("take server", id="take-server")
                yield Button("view diff", id="view-diff")
                yield Button("cancel", id="cancel")
            with Horizontal(id="back-row"):
                yield Button("back", id="back")

    def on_mount(self) -> None:
        self.query_one("#diff-pane").display = False
        self.query_one("#back-row").display = False

    @on(Button.Pressed, "#keep-mine")
    def _on_keep_mine(self) -> None:
        self._keep_mine()

    @on(Button.Pressed, "#take-server")
    def _on_take_server(self) -> None:
        self._take_server()

    @on(Button.Pressed, "#view-diff")
    def _on_view_diff(self) -> None:
        self._diff()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.query_one("#diff-pane").display = False
        self.query_one("#choices").display = True
        self.query_one("#back-row").display = False

    def show_diff(self, text: str) -> None:
        """Replace the choices with the unified diff; `back` returns to them."""
        self.query_one("#diff-text", Static).update(text or "(no differences)")
        self.query_one("#diff-pane").display = True
        self.query_one("#choices").display = False
        self.query_one("#back-row").display = True


class WriterApp(App):
    """The writing app.

    `client` is anything `run_sync` accepts — `list_entries_full()`,
    `list_assets()`, `download_asset()` — plus `upsert_entry()` and
    `upload_asset()` for publishing. Leaving it `None` builds a real one
    from the environment when the first sync runs. Tests pass a stub and
    never open a socket.
    """

    CSS_PATH = "app.tcss"
    TITLE = "log"

    BINDINGS = [
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+r", "sync", "sync"),
        Binding("ctrl+t", "toggle_tab", "draft/meta"),
        Binding("ctrl+g", "focus_sidebar", "drafts"),
        Binding("ctrl+n", "new_draft", "new"),
        Binding("ctrl+l", "preview", "preview"),
        Binding("ctrl+b", "publish", "publish"),
    ]

    def __init__(
        self,
        client=None,
        prober=None,
        browser_opener=None,
        start_server=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._client = client
        self.current_path: Path | None = None
        self.current_draft: Draft | None = None
        # The sidecar as of the last load: slug -> {hash, date,
        # assets_hash, state}. Reloaded from disk after every sync, and
        # its "state" label kept live between syncs by `_mark_current`,
        # which writes each change through. It is the one record the
        # sidebar reads: no marker lives only in memory.
        self.sync_state: dict = {}
        self._offline = False
        self._parse_error: str | None = None
        # The editors' text at the last moment they agreed with the file:
        # set when a draft is loaded in, and again after every successful
        # save. Anything else in the editors is work nobody has written
        # down, which is what makes a reload dangerous.
        self._editor_baseline: str | None = None
        # The slug whose file moved under the editor. Autosave is refused
        # for it until the row is deliberately reopened — see
        # `_file_changed_under_editor`.
        self._stale_slug: str | None = None
        self._suppress_changes = 0
        self._new_draft_path: Path | None = None
        self._probe = prober if prober is not None else _default_probe
        self._open_browser = browser_opener if browser_opener is not None else webbrowser.open
        self._start_server = start_server if start_server is not None else _default_start_server
        self._server_process: subprocess.Popen | None = None

    # --- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="frame"):
            yield ListView(id="sidebar")
            with Vertical(id="editor"):
                with TabbedContent(initial="draft", id="tabs"):
                    with TabPane("draft", id="draft"):
                        yield TextArea(id="body", soft_wrap=True)
                    with TabPane("meta", id="meta"):
                        yield TextArea(id="header", soft_wrap=True)
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.sync_state = load_state(_state_path())
        await self.refresh_sidebar()
        self.query_one("#sidebar", ListView).focus()
        self.sync_now()

    # --- the seam other screens use -------------------------------------

    @property
    def suppressed_changes(self) -> int:
        """Programmatic editor changes still owed a `Changed` event."""
        return self._suppress_changes

    @property
    def published_slugs(self) -> set[str]:
        """Every slug the mirror has a base for — what the server has.

        Derived, never assigned: the sidecar is the one record of what
        the server holds, so there is no second list to keep in step
        with it.
        """
        return set(self.sync_state)

    def _base(self, slug: str) -> dict | None:
        """The sidecar's record for `slug`, or `None` if it has no base.

        Guards the shape as well as the presence: `load_state` promises
        only that the sidecar is a dict, so an entry that isn't one is
        treated as no entry rather than crashing a redraw.
        """
        entry = self.sync_state.get(slug)
        return entry if isinstance(entry, dict) else None

    def _entry_state(self, slug: str) -> str:
        """`clean` / `edited` / `conflict` / `local-only` for one slug."""
        base = self._base(slug)
        if base is None:
            return "local-only"
        state = base.get("state")
        return state if state in _MARKERS else "clean"

    def _mirrored(self, slug: str) -> bool:
        """Does this slug belong to the log section rather than drafts?

        Having any sidecar entry is the whole test — including the
        state-only one the engine records for a conflict it has no base
        for, which is why a ⚠ row is still a log row after a restart.
        """
        return self._base(slug) is not None

    def load_draft_into_editor(self, path: Path) -> None:
        """Fill both editors from a draft file, header bytes and all.

        A file that no longer parses is still opened — that is how it gets
        fixed — but it leaves no current draft, the gauge says why, and
        nothing is written back over it until it parses again.
        """
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self._clear_editor()
            self._show_error(f"cannot read {path.name}")
            return

        # Reading the file in is the deliberate act that answers a
        # "changed under the editor" gate: whatever was being held is
        # now replaced by what the file says, on purpose.
        self._stale_slug = None
        header, body = split_source(text)
        self.current_path = path
        self._set_editor_text(header, body)

        try:
            draft = parse_draft(text)
        except DraftError as error:
            self.current_draft = None
            self._show_error(str(error))
            return
        draft.path = path
        self.current_draft = draft
        self._parse_error = None
        self._show_state()

    def _clear_editor(self) -> None:
        """No draft in hand: empty editors, nothing to be wrong about."""
        self.current_path = None
        self.current_draft = None
        self._parse_error = None
        self._stale_slug = None
        self._set_editor_text("", "")

    async def refresh_sidebar(self) -> None:
        """Rebuild both sections from the files on disk and the sidecar.

        Drafts first — the files no sync has ever claimed — then the log,
        newest first by the date the sidecar recorded, each row wearing
        its state's marker. The open draft survives the rebuild when its
        file is still there; otherwise the first openable row is taken
        up. A rebuild redraws the gauge but never re-judges it — text
        that failed to parse is still in the editors, and still says so.
        """
        sidebar = self.query_one("#sidebar", ListView)
        keep = self.current_path

        items: list[SidebarItem] = [
            SidebarItem(f"{_LOCAL_ONLY}{path.stem}", kind="draft", draft_path=path, slug=path.stem)
            for path in list_drafts()
            if not self._mirrored(path.stem)
        ]
        items.append(SidebarItem(_DIVIDER, kind="divider", disabled=True))
        items.extend(self._log_rows())

        await sidebar.clear()
        await sidebar.extend(items)

        # A row for a file that isn't there (a mirrored entry deleted
        # locally while offline) still shows — the server has it, and
        # the next sync brings it back — but it is never what a rebuild
        # opens by itself.
        openable = [
            i for i, item in enumerate(items)
            if item.draft_path is not None and item.draft_path.exists()
        ]
        if not openable:
            self._clear_editor()
            self._show_state()
            return

        target = next((i for i in openable if items[i].draft_path == keep), openable[0])
        sidebar.index = target
        if self.current_path != items[target].draft_path:
            self.load_draft_into_editor(items[target].draft_path)
        else:
            self._show_state()

    def _log_rows(self) -> list[SidebarItem]:
        """The log section: every mirrored entry, plus why it's empty."""
        root = drafts_dir()
        rows = [
            SidebarItem(
                self._log_label(slug),
                kind="log",
                draft_path=root / f"{slug}.md",
                slug=slug,
            )
            for slug in self._log_slugs()
        ]
        if self._offline:
            # Not an error — the markers below still say whatever the
            # last sync knew, and `ctrl+r` retries.
            rows.insert(0, SidebarItem("(offline)", kind="note", disabled=True))
        elif not rows:
            rows.append(SidebarItem("(nothing in the log)", kind="note", disabled=True))
        return rows

    def _log_slugs(self) -> list[str]:
        """Mirrored slugs, newest first by the date the sidecar recorded.

        A conflict recorded with no base has no date to sort by; it
        sorts last rather than being left out, since a row nobody can
        see is a conflict nobody can resolve.
        """
        slugs = [slug for slug in self.sync_state if self._mirrored(slug)]
        return sorted(slugs, key=self._log_sort_key, reverse=True)

    def _log_sort_key(self, slug: str) -> tuple[str, str]:
        base = self._base(slug)
        return (str(base.get("date") or "") if base else "", slug)

    def _log_label(self, slug: str) -> str:
        return f"{_MARKERS.get(self._entry_state(slug), '')}{slug}"

    # --- the mirror ------------------------------------------------------

    def sync_now(self) -> None:
        """Start a sync, reading the open-editor guard first.

        The guard's two inputs are widget state, so they are taken here,
        on the UI thread, and handed to the worker — which must never
        look at a widget itself.
        """
        open_slug, open_clean = self._open_guard()
        self._sync(open_slug, open_clean)

    def _open_guard(self) -> tuple[str | None, bool]:
        """The open slug, and whether its editor is safe to overwrite.

        Clean means both halves of the claim: the editors parse, and
        what they hold is what the file holds. Anything else — unsaved
        text that doesn't parse, an unreadable file — is dirty, and a
        server-newer update to that slug becomes a conflict instead of a
        write.
        """
        if self.current_path is None:
            return None, True
        slug = self.current_path.stem
        if self._parse_error is not None:
            return slug, False
        try:
            on_disk = self.current_path.read_text(encoding="utf-8")
        except OSError:
            return slug, False
        return slug, self._editor_text() == on_disk

    @work(thread=True, group="sync", exclusive=True)
    def _sync(self, open_slug: str | None, open_clean: bool) -> None:
        """Mirror the server into the drafts directory. Off the UI thread."""
        client = self._client if self._client is not None else default_client()
        try:
            report = run_sync(
                client, drafts_dir(), open_slug=open_slug, open_clean=open_clean
            )
        except ClientError:
            self.call_from_thread(self._sync_unavailable)
        except Exception:
            # The client's contract is that every failure is a ClientError.
            # A client that breaks it — or a stand-in that never had it —
            # still must not take a writing session down: an unfailable
            # worker is the only kind this app can afford.
            self.call_from_thread(self._sync_unavailable)
        else:
            self.call_from_thread(self._sync_arrived, report, open_slug, open_clean)

    async def _sync_arrived(
        self, report: SyncReport, open_slug: str | None, open_clean: bool
    ) -> None:
        """Take up what the sync wrote: sidecar, markers, editor, one line."""
        self._offline = False
        self.sync_state = load_state(_state_path())
        rewritten = set(report.new) | set(report.updated) | set(report.adopted)
        await self.refresh_sidebar()
        # A file the sync rewrote under the open editor has to be taken
        # up again, or the next keystroke would save what the editor
        # still holds back over it. The question is asked of whatever is
        # open *now* and of that editor's own baseline — not of the slug
        # the guard measured when the sync started. A row switched into
        # while the worker ran is exactly as exposed, and worse off: the
        # engine's guard never covered it, so nothing else will notice.
        # The disk-differs check only skips a pointless reload, and the
        # cursor jump with it, when the rewrite changed nothing on screen.
        slug = self.current_path.stem if self.current_path is not None else None
        if slug is not None and slug in rewritten:
            if not self._editor_is_clean():
                self._file_changed_under_editor(slug)
            else:
                try:
                    on_disk = self.current_path.read_text(encoding="utf-8")
                except OSError:
                    on_disk = None
                if on_disk is not None and on_disk != self._editor_text():
                    self.load_draft_into_editor(self.current_path)
        self.notify(_sync_line(report))
        if report.errors:
            self.notify(_error_line(report.errors), severity="error")

    def _file_changed_under_editor(self, slug: str) -> None:
        """A sync rewrote the open file while the editor held unsaved text.

        Two real versions now exist — the server's, on disk, and the
        poet's, still on screen — so this writes neither of them
        anywhere. The row is marked ⚠ so the state is visible after a
        restart too, the gauge says why, one line names the slug, and
        autosave for that file is refused until the row is deliberately
        reopened. Refused, not silently dropped: the alternative is the
        next keystroke saving the older text over what just arrived,
        which the following sync would read as an ordinary local edit and
        never question.

        A later sync finds the file agreeing with the server and calls
        the row clean again; the refusal is this session's, and holds
        until the text on screen is dealt with either way.
        """
        self._stale_slug = slug
        base = self._base(slug)
        if base is not None and base.get("state") != "conflict":
            base["state"] = "conflict"
            self._persist_state(slug, "conflict")
            self._relabel_row(slug)
        self._show_error(_STALE_MESSAGE)
        self.notify(
            f"{slug} changed on disk under the editor — the editors still hold "
            "the older text; reopen the row to take the newer",
            severity="warning",
        )

    async def _sync_unavailable(self) -> None:
        """Offline is quiet: the mirror stands, the markers stand, no noise."""
        self._offline = True
        await self.refresh_sidebar()

    # --- selection ------------------------------------------------------

    @on(ListView.Highlighted, "#sidebar")
    def _sidebar_highlighted(self, event: ListView.Highlighted) -> None:
        self._take_up(event.item)

    @on(ListView.Selected, "#sidebar")
    def _sidebar_selected(self, event: ListView.Selected) -> None:
        self._take_up(event.item)
        if self.current_draft is not None:
            self.query_one("#body", TextArea).focus()

    def _take_up(self, item: ListItem | None) -> None:
        """Load the file a sidebar row stands for, if it isn't open yet.

        Both sections open the same way — a mirrored entry is a local
        file like any other — with one exception: a `⚠` row opens
        `ConflictModal` instead, since a conflict is a real fork the poet
        has to choose about, not something a plain row-select should
        paper over. Open, not saved: a draft whose header is currently
        broken is still the open one, and re-highlighting its row must
        not quietly reload the file underneath the text being fixed.
        """
        if not isinstance(item, SidebarItem) or item.kind not in ("draft", "log"):
            return
        if self.current_path == item.draft_path:
            return
        if item.kind == "log" and self._entry_state(item.slug) == "conflict":
            self._open_conflict(item.slug, item.draft_path)
            return
        self.load_draft_into_editor(item.draft_path)

    @work
    async def _open_conflict(self, slug: str, path: Path) -> None:
        """Run the conflict modal for `slug`, then open its file either way.

        `push_screen_wait` needs an active worker to await on, the same
        reason `action_new_draft`/`action_publish`/`action_preview` are
        `@work` — the asyncio-task flavor, not `thread=True` — without
        moving anything off the UI thread itself; the network happens in
        the modal's own thread workers below. Every way out of the modal
        — keep mine, take server, or cancel — ends the same way: the
        file loads into the editor and the sidebar is rebuilt from
        whatever the sidecar now says, so the poet is looking at a real,
        open draft regardless of which way the conflict was settled.
        """
        modal = ConflictModal(
            slug,
            lambda: self._conflict_keep_mine(slug, modal),
            lambda: self._conflict_take_server(slug, path, modal),
            lambda: self._conflict_diff(slug, path, modal),
        )
        await self.push_screen_wait(modal)
        self.load_draft_into_editor(path)
        await self.refresh_sidebar()

    @work(thread=True, group="conflict", exclusive=True)
    def _conflict_keep_mine(self, slug: str, modal: ConflictModal) -> None:
        """KEEP MINE: advance the sidecar base to the server's values.

        The file itself is never touched — keeping "mine" means keeping
        the poet's text on disk exactly as it is; only the recorded base
        moves, to the server's `(hash, date)`, so the row reads `●`
        (edited against that new base) rather than `⚠`.
        """
        client = self._client if self._client is not None else default_client()
        try:
            entry = client.get_entry(slug)
        except ClientError as error:
            self.call_from_thread(self._conflict_fetch_failed, str(error))
            return
        self.call_from_thread(self._conflict_keep_mine_apply, slug, entry, modal)

    def _conflict_keep_mine_apply(self, slug: str, entry: dict, modal: ConflictModal) -> None:
        self._advance_conflict_base(
            slug,
            hash=content_hash(entry["title"], entry["content_markdown"]),
            date=entry["publish_date"],
            state="edited",
        )
        modal.dismiss(None)

    @work(thread=True, group="conflict", exclusive=True)
    def _conflict_take_server(self, slug: str, path: Path, modal: ConflictModal) -> None:
        """TAKE SERVER: rewrite the file from the server entry, same shape as AUTO_UPDATE."""
        client = self._client if self._client is not None else default_client()
        try:
            entry = client.get_entry(slug)
        except ClientError as error:
            self.call_from_thread(self._conflict_fetch_failed, str(error))
            return
        self.call_from_thread(self._conflict_take_server_apply, slug, path, entry, modal)

    def _conflict_take_server_apply(
        self, slug: str, path: Path, entry: dict, modal: ConflictModal
    ) -> None:
        draft = Draft(
            title=entry["title"], slug=slug, date=entry["publish_date"],
            body=entry["content_markdown"], path=path,
        )
        path.write_text(serialize_draft(draft), encoding="utf-8")
        self._advance_conflict_base(
            slug,
            hash=content_hash(entry["title"], entry["content_markdown"]),
            date=entry["publish_date"],
            state="clean",
        )
        modal.dismiss(None)

    @work(thread=True, group="conflict", exclusive=True)
    def _conflict_diff(self, slug: str, path: Path, modal: ConflictModal) -> None:
        """VIEW DIFF: fetch the server entry and hand the modal the unified diff text."""
        client = self._client if self._client is not None else default_client()
        try:
            entry = client.get_entry(slug)
        except ClientError as error:
            self.call_from_thread(self._conflict_fetch_failed, str(error))
            return
        text = self._conflict_diff_text(path, entry)
        self.call_from_thread(modal.show_diff, text)

    def _conflict_fetch_failed(self, message: str) -> None:
        """A `ClientError` fetching the server row: say so, change nothing."""
        self.notify(message, severity="error")

    def _advance_conflict_base(self, slug: str, *, hash: str, date: str | None, state: str) -> None:
        """Write a slug's sidecar base — hash, date, state — through to disk.

        Load-modify-save against the sidecar as it stands right now, the
        same discipline `_persist_state` uses: a sync may have rewritten
        it since this session last read it. `assets_hash` is never set or
        cleared here — it is a separate slice of the base, untouched by a
        conflict resolution either way. Builds the entry from nothing
        when the recorded conflict was state-only (no base at all): that
        is exactly the hole a resolution closes.
        """
        path = _state_path()
        on_disk = load_state(path)
        entry = on_disk.get(slug)
        if not isinstance(entry, dict):
            entry = {}
        entry["hash"] = hash
        entry["date"] = date
        entry["state"] = state
        on_disk[slug] = entry
        save_state(path, on_disk)
        self.sync_state = on_disk

    def _conflict_diff_text(self, path: Path, entry: dict) -> str:
        """The full unified diff of `title + body`, local vs. server. No truncation."""
        local_title, local_body = self._local_diff_halves(path)
        local_lines = (local_title + "\n" + local_body).splitlines(keepends=True)
        server_text = entry["title"] + "\n" + entry["content_markdown"]
        server_lines = server_text.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(local_lines, server_lines, fromfile="local", tofile="server")
        )

    def _local_diff_halves(self, path: Path) -> tuple[str, str]:
        """The local side of a conflict diff: parsed title/body, or the raw split.

        A file that no longer parses still has something to diff against
        — the same header/body split the meta pane itself falls back to
        (`split_source`) — so the diff stays informational rather than
        refusing outright. Neither keep-mine nor take-server needs this:
        the base they advance to comes entirely from the server row.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return "", ""
        try:
            draft = parse_draft(text)
        except DraftError:
            return split_source(text)
        return draft.title, draft.body

    # --- editing --------------------------------------------------------

    def _set_editor_text(self, header: str, body: str) -> None:
        """Fill both editors without that counting as an edit.

        Two assignments, two increments — `TextArea.load_text` posts
        exactly one `Changed` for each. What was just put in is also the
        editors' new baseline: read back through `_editor_text` rather
        than reassembled here, so the two can never drift apart.
        """
        header_area = self.query_one("#header", TextArea)
        body_area = self.query_one("#body", TextArea)
        self._suppress_changes += 1
        header_area.text = header
        self._suppress_changes += 1
        body_area.text = body
        self._editor_baseline = self._editor_text()

    def _editor_text(self) -> str:
        """The draft file the two editors currently spell out."""
        header = self.query_one("#header", TextArea).text.rstrip("\n")
        body = self.query_one("#body", TextArea).text
        return f"{header}\n\n{body}"

    def _editor_is_clean(self) -> bool:
        """Has the editor gone untouched since it was filled or last saved?

        Its own baseline, not the file — a different question from
        `_open_guard`'s. That one asks whether the engine may rewrite the
        file, which is about disk; this asks whether taking the file back
        up would carry off work nobody has written down, which is about
        the editor. A file rewritten under an untouched editor is safe to
        reload; the same rewrite under unsaved text is not.
        """
        return (
            self._editor_baseline is not None
            and self._editor_text() == self._editor_baseline
        )

    @on(TextArea.Changed, "#body")
    @on(TextArea.Changed, "#header")
    def _editor_changed(self, event: TextArea.Changed) -> None:
        if self._suppress_changes > 0:
            self._suppress_changes -= 1
            return
        self.save_current()

    def save_current(self) -> bool:
        """Parse the editors and write the draft. False if it didn't parse.

        The file being edited keeps its name: a slug changed in the meta
        pane is written into the file it was changed in, not to a new one.

        One thing outranks a parse: a file that moved under the editor
        (see `_file_changed_under_editor`) is not written to at all, so
        that the older text still on screen cannot bury what arrived.
        """
        if (
            self._stale_slug is not None
            and self.current_path is not None
            and self.current_path.stem == self._stale_slug
        ):
            self._show_error(_STALE_MESSAGE)
            return False
        try:
            draft = parse_draft(self._editor_text())
        except DraftError as error:
            self._show_error(str(error))
            return False
        draft.path = self.current_path
        save_draft(draft)
        self.current_path = draft.path
        self.current_draft = draft
        self._parse_error = None
        # What is on screen is now what the file holds — cosmetically
        # different, perhaps (the save canonicalizes the header), but
        # nothing here is unwritten work any more.
        self._editor_baseline = self._editor_text()
        self._mark_current()
        self._show_state()
        return True

    def _mark_current(self) -> None:
        """Re-judge the saved draft against its base, and re-label its row.

        Between syncs the sidecar's `state` is this app's to keep true:
        the moment a mirrored entry stops matching what the server last
        gave, its row says `●`, without waiting for a round trip — and
        the sidecar is written through, so the mark is still there after
        a restart with no server to ask. Only the label moves — hash and
        date are the sync's to advance — and a `⚠` is left alone, since
        only resolving it can clear it.

        Keyed by the file's name, not the header's `slug:`, because that
        is the key the sync engine records a base under.
        """
        if self.current_path is None or self.current_draft is None:
            return
        slug = self.current_path.stem
        base = self._base(slug)
        if base is None or base.get("state") == "conflict":
            return
        draft = self.current_draft
        matches = (
            base.get("hash") == content_hash(draft.title, draft.body)
            and str(base.get("date") or "") == str(draft.date or "")
        )
        state = "clean" if matches else "edited"
        if base.get("state") == state:
            return
        base["state"] = state
        self._persist_state(slug, state)
        self._relabel_row(slug)

    def _persist_state(self, slug: str, label: str) -> None:
        """Write one row's state label through to the sidecar on disk.

        Load-modify-save, not a write of `self.sync_state` wholesale: a
        sync may have rewritten the file since this app last read it, and
        this one label is the only part of it the editor owns. Called
        only on a transition, never per keystroke.

        A sidecar that cannot be written is not worth losing a keystroke
        over: the marker still stands for this session, and the next sync
        recomputes the label from the file itself.
        """
        path = _state_path()
        try:
            on_disk = load_state(path)
            entry = on_disk.get(slug)
            if not isinstance(entry, dict) or entry.get("state") == label:
                return
            entry["state"] = label
            save_state(path, on_disk)
        except OSError:
            return

    def _relabel_row(self, slug: str) -> None:
        """Redraw one log row's marker in place, without a rebuild.

        A rebuild would reset the sidebar's cursor and re-open a file
        mid-keystroke; a marker changing is not worth either.
        """
        label = self._log_label(slug)
        for item in self.query_one("#sidebar", ListView).children:
            if isinstance(item, SidebarItem) and item.kind == "log" and item.slug == slug:
                item.label = label
                item.query_one(Static).update(label)
                return

    # --- the status line ------------------------------------------------

    def _show_state(self) -> None:
        """Redraw the gauge from the last parse outcome.

        The ✗ outlives whatever redrew the line — a sidebar rebuild, a
        sync landing — because the text that earned it is still in the
        editors and still unsaved.
        """
        if self._parse_error is not None:
            self._status(f"✗ {self._parse_error}")
            return
        if self.current_draft is None:
            self._status("no draft")
            return
        words = len(self.current_draft.body.split())
        parts = [f"✓ {words} words"]
        if self.current_draft.slug in self.published_slugs:
            parts.append("on server")
        parts.extend(self.current_draft.warnings)
        self._status(" · ".join(parts))

    def _show_error(self, message: str) -> None:
        """Record why nothing can be written right now, and say so.

        Usually a parse failure; sometimes a file that moved under the
        editor. Recorded, not just printed: every later redraw reads it
        back, so nothing can paint a ✓ over text that is not going to
        disk.
        """
        self._parse_error = message
        self._show_state()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # --- actions --------------------------------------------------------

    def action_save_now(self) -> None:
        self.save_current()

    async def action_sync(self) -> None:
        """Rebuild from disk now, and ask the server for the rest."""
        await self.refresh_sidebar()
        self.sync_now()

    def action_toggle_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "meta" if tabs.active == "draft" else "draft"
        self.query_one("#header" if tabs.active == "meta" else "#body", TextArea).focus()

    def action_focus_sidebar(self) -> None:
        self.query_one("#sidebar", ListView).focus()

    @work
    async def action_new_draft(self) -> None:
        """Open the new-draft modal; on success, load and select the file.

        `push_screen_wait` requires an active Textual worker to await on —
        `@work` (the default asyncio-task flavor, not `thread=True`) makes
        this action one, without moving anything off the UI thread.
        """
        created = await self.push_screen_wait(NewDraftModal(self._attempt_new_draft))
        if created:
            assert self._new_draft_path is not None
            self.load_draft_into_editor(self._new_draft_path)
            await self.refresh_sidebar()

    def _attempt_new_draft(self, title: str, slug: str, date: str | None) -> str | None:
        """Validate a new draft's fields and, if valid, create it.

        Returns an error message (the modal stays open and shows it) or
        `None` on success, in which case `self._new_draft_path` names the
        file just written.
        """
        if not title:
            return "title is required"
        if not _SLUG_RE.fullmatch(slug):
            return f"invalid slug {slug!r} — use lowercase letters, digits, hyphens"
        if (drafts_dir() / f"{slug}.md").exists():
            return f"a draft named {slug!r} already exists"
        if slug in self.published_slugs:
            # Normally unreachable — a published slug has a mirrored file,
            # caught above. It survives for the case where that file was
            # deleted locally: the entry is still the server's, and a new
            # draft under its name would collide on the next publish.
            return f"{slug!r} is already published"
        draft = Draft(title=title, slug=slug, date=date, body="", path=None)
        save_draft(draft)
        assets_dir(draft).mkdir(parents=True, exist_ok=True)
        self._new_draft_path = draft.path
        return None

    @work
    async def action_preview(self) -> None:
        """Open the draft's preview URL, starting the dev server if needed."""
        if self.current_draft is None:
            return
        url = f"{PREVIEW_HOST}/draft-preview/{self.current_draft.slug}/"
        if self._probe():
            self._open_browser(url)
            return
        start = await self.push_screen_wait(StartServerModal())
        if not start:
            return
        self._server_process = self._start_server()
        self._open_browser(url)

    # --- publish ----------------------------------------------------------

    @work
    async def action_publish(self) -> None:
        """Open the publish modal for the open draft.

        `push_screen_wait` requires an active worker to await on, same as
        `action_new_draft` and `action_preview` — see those for why `@work`
        (the asyncio-task flavor) rather than `thread=True` is right here:
        nothing in this method itself touches the network.

        Failure rule 8: a slug still marked `⚠` is refused here, before
        the modal ever opens and so before any client call — publishing
        one side of an unresolved fork would bury the other. The status
        line names the slug rather than notifying, matching every other
        precondition this app checks synchronously (compare
        `_attempt_new_draft`'s error path).
        """
        if self.current_draft is None:
            return
        draft = self.current_draft
        if self._entry_state(draft.slug) == "conflict":
            self._status(f"resolve the conflict on {draft.slug} first")
            return
        statement = self._publish_statement(draft)
        await self.push_screen_wait(
            PublishModal(statement, lambda b, m: self._attempt_publish(draft, b, m))
        )

    def _publish_statement(self, draft: Draft) -> str:
        """`creates new entry "<slug>"`, or `updates "<title>" from <year>`.

        Drawn from the mirror, not a fresh request: a slug the sidecar
        has a base for is one the server already has, and the year comes
        from the date that base recorded. Publishing must not block the
        modal on a network call just to word its own confirmation.
        """
        base = self._base(draft.slug)
        if base is None:
            return f'creates new entry "{draft.slug}"'
        year = str(base.get("date") or "")[:4]
        if not year:
            return f'updates "{draft.title}"'
        return f'updates "{draft.title}" from {year}'

    def _attempt_publish(
        self, draft: Draft, share_bluesky: bool, share_mastodon: bool
    ) -> str | None:
        """The modal's `attempt`: only the token precondition runs here.

        Everything that touches the network — every asset upload, then the
        entry upsert — happens in `_publish`'s thread worker, kicked off
        below and left running after this returns. A message here keeps the
        modal open (mirrors `_attempt_new_draft`); `None` dismisses it, and
        the publish continues in the background regardless of how it turns
        out — success and failure are both reported by notification, since
        by then this modal is already gone.
        """
        base_url = os.environ.get("BLOG_WRITER_BASE_URL", DEFAULT_BASE_URL)
        token = os.environ.get("BLOG_WRITER_TOKEN")
        if not token and not _is_local(base_url):
            return "BLOG_WRITER_TOKEN is not set"
        self._publish(draft, share_bluesky, share_mastodon)
        return None

    @work(thread=True, group="publish", exclusive=True)
    def _publish(self, draft: Draft, share_bluesky: bool, share_mastodon: bool) -> None:
        """Upload assets and upsert the entry. Never touches the draft file.

        Ordering branches on whether the entry already exists — the same
        mirror `_publish_statement` reads, checked fresh here rather
        than a value snapshotted before the modal opened. A brand
        new slug has no `LogEntry` row on the server yet, and `LogAsset.
        log_entry` is a required foreign key to it (`website/models.py`),
        so the server 404s ("unknown entry") on any asset upload attempted
        before that row exists — the create case MUST upsert first. An
        already-published slug keeps assets-first, which protects the live
        page from markdown that references an asset that hasn't landed.
        """
        client = self._client if self._client is not None else default_client()
        if draft.slug in self.published_slugs:
            self._publish_update(client, draft, share_bluesky, share_mastodon)
        else:
            self._publish_create(client, draft, share_bluesky, share_mastodon)

    def _publish_update(
        self, client, draft: Draft, share_bluesky: bool, share_mastodon: bool
    ) -> None:
        """Already-published entries: assets first, then the upsert."""
        uploaded, failure = self._upload_assets(client, draft)
        if failure is not None:
            self.call_from_thread(self._publish_failed, uploaded, failure)
            return
        try:
            result = client.upsert_entry(
                draft.slug,
                draft.title,
                draft.body,
                publish_date=draft.date,
                share_bluesky=share_bluesky,
                share_mastodon=share_mastodon,
            )
        except ClientError as error:
            self.call_from_thread(self._publish_failed, uploaded, str(error))
            return
        verb = result.get("status", "updated")
        self.call_from_thread(self._publish_succeeded, draft.slug, verb)

    def _publish_create(
        self, client, draft: Draft, share_bluesky: bool, share_mastodon: bool
    ) -> None:
        """Brand-new entries: the upsert first (it creates the row), then assets."""
        try:
            result = client.upsert_entry(
                draft.slug,
                draft.title,
                draft.body,
                publish_date=draft.date,
                share_bluesky=share_bluesky,
                share_mastodon=share_mastodon,
            )
        except ClientError as error:
            self.call_from_thread(self._publish_failed, [], str(error))
            return
        verb = result.get("status", "updated")
        uploaded, failure = self._upload_assets(client, draft)
        if failure is not None:
            self.call_from_thread(
                self._publish_entry_ok_asset_failed, draft.slug, verb, uploaded, failure
            )
            return
        self.call_from_thread(self._publish_succeeded, draft.slug, verb)

    def _upload_assets(self, client, draft: Draft) -> tuple[list[str], str | None]:
        """Upload every file in `<slug>.assets/`, stopping at the first failure.

        Returns the names that landed, in upload order, and — if one
        raised — a one-line message that names *that* file alongside the
        server's own truth about why (a `ClientError`'s message does not
        reliably name the file itself, so this always prefixes it).
        """
        uploaded: list[str] = []
        assets = assets_dir(draft)
        if assets.is_dir():
            for path in sorted(p for p in assets.iterdir() if p.is_file()):
                try:
                    client.upload_asset(draft.slug, path.name, path.read_bytes())
                except ClientError as error:
                    return uploaded, f"{path.name}: {error}"
                uploaded.append(path.name)
        return uploaded, None

    def _publish_succeeded(self, slug: str, verb: str) -> None:
        """Say so, then sync — which is what moves the row into the log.

        A sync is the whole reconciliation: the entry that just landed
        and the file on disk now agree, so it adopts the slug and records
        the base the server's own answer gives it.
        """
        self.notify(f"published {slug} ({verb})")
        self.sync_now()

    def _publish_failed(self, uploaded: list[str], message: str) -> None:
        """Report a publish failure before any entry existed on the server.

        Uploads are idempotent (the server treats a byte-identical re-upload
        as `unchanged`), so anything already uploaded is safe to send again;
        the message says so rather than leaving the poet to guess whether a
        retry will duplicate work.
        """
        if uploaded:
            message = f"{message} — uploaded {', '.join(uploaded)}; re-publishing is safe"
        self.notify(message, severity="error")

    def _publish_entry_ok_asset_failed(
        self, slug: str, verb: str, uploaded: list[str], failure: str
    ) -> None:
        """Report a create-case failure where the entry itself already landed.

        Unlike `_publish_failed`, the entry exists on the server by this
        point regardless of how the assets went, so re-publishing is always
        safe here — a retry sees the slug as published and takes the
        assets-first update path, which converges — even if zero assets
        made it up before the failure.
        """
        landed = f"uploaded {', '.join(uploaded)}" if uploaded else "no assets uploaded yet"
        self.notify(
            f"entry {slug} {verb} but {failure} — {landed}; re-publishing is safe",
            severity="error",
        )

    async def on_unmount(self) -> None:
        """Stop a dev server this session started, so it doesn't outlive it."""
        if self._server_process is not None:
            self._server_process.terminate()
