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
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
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
    list_drafts,
    parse_draft,
    save_draft,
)

DEFAULT_BASE_URL = "https://williamhazard.co"

_DIVIDER = "── published ──"


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
    ]

    def __init__(self, client=None, **kwargs):
        super().__init__(**kwargs)
        self._client = client
        self.current_path: Path | None = None
        self.current_draft: Draft | None = None
        self.published_slugs: set[str] = set()
        self._published: list[dict] | None = None
        self._offline = False
        self._parse_error: str | None = None
        self._suppress_changes = 0

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
                disabled=True,
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
