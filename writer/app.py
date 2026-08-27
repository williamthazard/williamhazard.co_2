"""The terminal app: a list of drafts, a two-tab editor, a status line.

The layout is a sidebar of draft files on the left (below a divider, the
entries the server already has), an editor on the right with a `draft` tab
for the body and a `meta` tab for the `key: value` header, and one status
line along the bottom.

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

The published half of the sidebar is fetched in a thread worker. A server
that cannot be reached — or that will not answer without a token — is an
ordinary condition, not an error: the section reads `(offline)` and the
app carries on as a local editor. Worker threads never touch a widget;
results come back through `call_from_thread`.
"""

from __future__ import annotations

import os
import re
import subprocess
import webbrowser
from pathlib import Path

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
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
)

DEFAULT_BASE_URL = "https://williamhazard.co"

# The repo root — parent of this `writer/` package — is where `manage.py`
# and the server's own `.venv/` (not this package's `writer/.venv/`) live.
REPO_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_HOST = "http://127.0.0.1:8000"

_DIVIDER = "── published ──"
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


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


class SidebarItem(ListItem):
    """One row of the sidebar.

    `kind` is what the row stands for — `draft`, `published`, or one of the
    non-selectable `divider` / `note` rows. A draft row carries the path of
    its file; a published row carries the slug the server knows it by.
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


class WriterApp(App):
    """The writing app.

    `client` is anything with `list_entries()`; leaving it `None` builds a
    real one from the environment when the fetch runs. Tests pass a stub
    and never open a socket.
    """

    CSS_PATH = "app.tcss"
    TITLE = "log"

    BINDINGS = [
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+r", "refresh", "refresh"),
        Binding("ctrl+t", "toggle_tab", "draft/meta"),
        Binding("ctrl+g", "focus_sidebar", "drafts"),
        Binding("ctrl+n", "new_draft", "new"),
        Binding("ctrl+l", "preview", "preview"),
        Binding("ctrl+f", "pull_to_draft", "pull"),
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
        self.published_slugs: set[str] = set()
        self._published: list[dict] | None = None
        self._offline = False
        self._parse_error: str | None = None
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
        await self.refresh_sidebar()
        self.query_one("#sidebar", ListView).focus()
        self.fetch_published()

    # --- the seam other screens use -------------------------------------

    @property
    def suppressed_changes(self) -> int:
        """Programmatic editor changes still owed a `Changed` event."""
        return self._suppress_changes

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
        self._set_editor_text("", "")

    async def refresh_sidebar(self) -> None:
        """Rebuild both halves of the sidebar from disk and last fetch.

        The highlighted draft survives the rebuild when its file is still
        there; otherwise the first draft is taken up. A rebuild redraws the
        gauge but never re-judges it — text that failed to parse is still
        in the editors, and still says so.
        """
        sidebar = self.query_one("#sidebar", ListView)
        keep = self.current_path

        items: list[SidebarItem] = [
            SidebarItem(path.stem, kind="draft", draft_path=path)
            for path in list_drafts()
        ]
        drafts_end = len(items)
        items.append(SidebarItem(_DIVIDER, kind="divider", disabled=True))
        items.extend(self._published_rows())

        await sidebar.clear()
        await sidebar.extend(items)

        if not drafts_end:
            self._clear_editor()
            self._show_state()
            return

        target = next(
            (i for i in range(drafts_end) if items[i].draft_path == keep),
            0,
        )
        sidebar.index = target
        if self.current_path != items[target].draft_path:
            self.load_draft_into_editor(items[target].draft_path)
        else:
            self._show_state()

    def _published_rows(self) -> list[SidebarItem]:
        if self._offline:
            return [SidebarItem("(offline)", kind="note", disabled=True)]
        if self._published is None:
            return [SidebarItem("(fetching)", kind="note", disabled=True)]
        if not self._published:
            return [SidebarItem("(nothing published)", kind="note", disabled=True)]
        return [
            SidebarItem(
                str(entry.get("slug", "")),
                kind="published",
                slug=str(entry.get("slug", "")),
            )
            for entry in self._published
        ]

    # --- the published half ---------------------------------------------

    @work(thread=True, group="published", exclusive=True)
    def fetch_published(self) -> None:
        """Ask the server what it has. Runs off the UI thread."""
        client = self._client if self._client is not None else default_client()
        try:
            entries = client.list_entries()
        except ClientError:
            self.call_from_thread(self._published_unavailable)
        except Exception:
            # The client's contract is that every failure is a ClientError.
            # A client that breaks it — or a stand-in that never had it —
            # still must not take a writing session down: an unfailable
            # worker is the only kind this app can afford.
            self.call_from_thread(self._published_unavailable)
        else:
            self.call_from_thread(self._published_arrived, entries)

    async def _published_arrived(self, entries: list[dict]) -> None:
        self._published = list(entries)
        self._offline = False
        self.published_slugs = {
            str(entry.get("slug", "")) for entry in self._published
        }
        await self.refresh_sidebar()

    async def _published_unavailable(self) -> None:
        self._published = None
        self._offline = True
        self.published_slugs = set()
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
        """Load the draft a sidebar row stands for, if it isn't open yet.

        Open, not saved: a draft whose header is currently broken is still
        the open one, and re-highlighting its row must not quietly reload
        the file underneath the text being fixed.
        """
        if not isinstance(item, SidebarItem) or item.kind != "draft":
            return
        if self.current_path == item.draft_path:
            return
        self.load_draft_into_editor(item.draft_path)

    # --- editing --------------------------------------------------------

    def _set_editor_text(self, header: str, body: str) -> None:
        """Fill both editors without that counting as an edit.

        Two assignments, two increments — `TextArea.load_text` posts
        exactly one `Changed` for each.
        """
        header_area = self.query_one("#header", TextArea)
        body_area = self.query_one("#body", TextArea)
        self._suppress_changes += 1
        header_area.text = header
        self._suppress_changes += 1
        body_area.text = body

    def _editor_text(self) -> str:
        """The draft file the two editors currently spell out."""
        header = self.query_one("#header", TextArea).text.rstrip("\n")
        body = self.query_one("#body", TextArea).text
        return f"{header}\n\n{body}"

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
        """
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
        self._show_state()
        return True

    # --- the status line ------------------------------------------------

    def _show_state(self) -> None:
        """Redraw the gauge from the last parse outcome.

        The ✗ outlives whatever redrew the line — a sidebar rebuild, a
        fetch landing — because the text that earned it is still in the
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
        """Record why the editors don't parse, and say so.

        Recorded, not just printed: every later redraw reads it back, so
        nothing can paint a ✓ over unsaved text that doesn't parse.
        """
        self._parse_error = message
        self._show_state()

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # --- actions --------------------------------------------------------

    def action_save_now(self) -> None:
        self.save_current()

    async def action_refresh(self) -> None:
        await self.refresh_sidebar()
        self.fetch_published()

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
            return f"{slug!r} is already published — use pull to draft instead"
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

    def action_pull_to_draft(self) -> None:
        """Fetch the highlighted published entry into a local draft file."""
        item = self.query_one("#sidebar", ListView).highlighted_child
        if not isinstance(item, SidebarItem) or item.kind != "published":
            return
        self._pull_to_draft(item.slug)

    @work(thread=True, group="pull", exclusive=True)
    def _pull_to_draft(self, slug: str) -> None:
        client = self._client if self._client is not None else default_client()
        try:
            entry = client.get_entry(slug)
        except ClientError as error:
            self.call_from_thread(self._status, str(error))
            return
        except Exception:
            # Same discipline as `fetch_published`: a client that breaks its
            # own contract must not take a writing session down.
            self.call_from_thread(self._status, f"could not pull {slug}")
            return
        path = drafts_dir() / f"{slug}.md"
        if path.exists():
            self.call_from_thread(
                self._status,
                f"{slug}.md already exists — pulling would overwrite local work",
            )
            return
        draft = Draft(
            title=str(entry.get("title", slug)),
            slug=slug,
            date=entry.get("publish_date"),
            body=str(entry.get("content_markdown", "")),
            path=None,
        )
        save_draft(draft)
        self.call_from_thread(self._pulled, draft.path)

    async def _pulled(self, path: Path) -> None:
        self.load_draft_into_editor(path)
        await self.refresh_sidebar()

    async def on_unmount(self) -> None:
        """Stop a dev server this session started, so it doesn't outlive it."""
        if self._server_process is not None:
            self._server_process.terminate()
