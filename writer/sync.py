"""The sync sidecar and the pure three-way classification at its heart.

`.sync.json` (see `load_state`/`save_state`) maps `slug -> {"hash",
"date", "assets_hash", "state"}` as of the last successful sync — the
recorded *base* against which a local file and a server entry are both
compared. `content_hash(title, body)` is the recipe both sides use:
sha256 over `title + "\\0" + body`, UTF-8 encoded, hex-digested. It is
computed from **parsed fields only** — never raw file bytes. Hashing a
file's bytes directly is forbidden: a save can change header spacing or
key order without changing title or body, and byte-hashing would read
that as a false modification. The server computes the identical recipe
over its own fields so the two sides compare directly.

`classify` is the state machine from the design's states table, kept
pure (nine tuple comparisons, no I/O) so it is unit-testable without a
TUI, a filesystem, or a network.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from writer.client import ClientError
from writer.draft import Draft, DraftError, parse_draft, serialize_draft


def content_hash(title: str, body: str) -> str:
    """sha256 hex digest of `title + "\\0" + body`, UTF-8 encoded."""
    return hashlib.sha256((title + "\0" + body).encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict:
    """Read the sidecar at `path`; `{}` if missing, unreadable, or invalid JSON.

    Never raises (failure rule 10: a missing or corrupt `.sync.json` is
    never fatal — every slug simply has no base, degrading to first-run
    behavior). Deleting the sidecar is always safe.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        state = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(state, dict):
        return {}
    return state


def save_state(path: Path, state: dict) -> None:
    """Write `state` to `path` as JSON, atomically.

    Writes to a temp file in the same directory, then `os.replace`s it
    into place, so a crash or concurrent read never observes a
    partially-written sidecar.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


class Action(enum.Enum):
    """The outcome of classifying one slug's local/base/server triple."""

    CLEAN = "clean"
    EDITED = "edited"
    AUTO_UPDATE = "auto_update"
    ADVANCE_BASE = "advance_base"
    CONFLICT = "conflict"
    LOCAL_ONLY = "local_only"
    NEW_ON_SERVER = "new_on_server"


def classify(
    *,
    local: tuple[str, str] | None,
    base: tuple[str, str] | None,
    server: tuple[str, str] | None,
) -> Action:
    """Classify one slug from its local, base, and server `(hash, date)` triple.

    Each argument is a `(content_hash, date_string)` tuple, or `None` if
    that side has no entry for the slug. "Changed vs base" means tuple
    inequality — either the hash or the date differs. Pure comparisons,
    no I/O; a direct transcription of the design's states table:

    | local vs base | server vs base | action |
    |---|---|---|
    | same | same | CLEAN |
    | changed | same | EDITED |
    | same | changed | AUTO_UPDATE |
    | changed | changed, identical to local | ADVANCE_BASE |
    | changed | changed differently | CONFLICT |
    | local exists | no server entry | LOCAL_ONLY |
    | no local | server entry exists | NEW_ON_SERVER |

    With no base entry (first run, adopted pre-existing draft, or a
    deleted sidecar), the degraded rule applies: local matches server
    exactly -> ADVANCE_BASE (adopt as clean); differs -> CONFLICT; no
    local file -> NEW_ON_SERVER; no server entry -> LOCAL_ONLY.
    """
    if server is None:
        return Action.LOCAL_ONLY

    if local is None:
        return Action.NEW_ON_SERVER

    if base is None:
        if local == server:
            return Action.ADVANCE_BASE
        return Action.CONFLICT

    local_changed = local != base
    server_changed = server != base

    if not local_changed and not server_changed:
        return Action.CLEAN
    if local_changed and not server_changed:
        return Action.EDITED
    if not local_changed and server_changed:
        return Action.AUTO_UPDATE
    # both changed
    if local == server:
        return Action.ADVANCE_BASE
    return Action.CONFLICT


# --- The I/O half: mirroring the server into `drafts_root` -----------------
#
# `run_sync` is the whole startup/ctrl+r sync. It fetches the entry list
# once, loads the sidecar once, classifies every slug the union of local
# files / server rows / prior sidecar entries mentions, and then acts:
#
# - NEW_ON_SERVER / unguarded AUTO_UPDATE / ADVANCE_BASE write the local
#   `.md` file and advance the recorded base (hash, date, assets_hash).
# - A guarded AUTO_UPDATE (the slug is open in the editor and the editor
#   isn't clean) demotes to a conflict instead of touching the file.
# - CONFLICT / EDITED / CLEAN only ever flip the sidecar's "state" label;
#   they never touch hash/date/assets_hash or the file.
# - LOCAL_ONLY drops a stale sidecar entry (its base refers to a server
#   row that no longer exists) and otherwise leaves everything alone.
# - An unparseable local file (`DraftError`) is treated as "modified,
#   content unknown": never auto-updated, CONFLICT if the server moved
#   since the base, EDITED otherwise.
#
# Asset reconciliation is a second, independent axis: for every slug the
# server still has, whenever its `assets_hash` differs from the recorded
# base (or there is no base yet), the asset list is fetched and any file
# absent locally is downloaded into `<slug>.assets/` — present files,
# whether matching or not, are always left alone, and nothing is ever
# deleted.


@dataclass
class SyncReport:
    """What one `run_sync` call did, in slugs (processing order)."""

    updated: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    assets_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


def run_sync(
    client,
    drafts_root: Path,
    *,
    open_slug: str | None = None,
    open_clean: bool = True,
    pace: float = 0.25,
) -> SyncReport:
    """Mirror every published entry into `drafts_root`.

    `client` is duck-typed against `WriterClient`: `list_entries_full()`,
    `list_assets(slug)`, `download_asset(slug, name)`. A `ClientError`
    from the initial `list_entries_full()` call propagates — the caller
    shows offline. A `ClientError` during a slug's asset reconciliation
    is instead recorded as a one-line truth in `report.errors`, and that
    slug's assets are skipped past; the rest of the sync continues.

    `open_slug`/`open_clean` are the open-editor guard: an AUTO_UPDATE
    targeting the currently-open, not-clean editor is marked ⚠ conflict
    instead of overwriting the file out from under the person editing
    it. `pace` is `time.sleep`'d between asset downloads.

    `save_state` is called exactly once, at the end.
    """
    rows = client.list_entries_full()

    drafts_root.mkdir(parents=True, exist_ok=True)
    state_path = drafts_root / ".sync.json"
    state = load_state(state_path)

    server_by_slug = {row["slug"]: row for row in rows}
    local_paths = {p.stem: p for p in sorted(drafts_root.glob("*.md"))}

    report = SyncReport()

    all_slugs = sorted(set(server_by_slug) | set(local_paths) | set(state))
    for slug in all_slugs:
        row = server_by_slug.get(slug)

        if row is None:
            # No server entry: LOCAL_ONLY, whatever the local file says.
            # A stale base (server-side deletion) is dropped; the file,
            # if any, is left alone either way.
            state.pop(slug, None)
            continue

        base_entry = state.get(slug)
        base = _base_tuple(base_entry)
        server_tuple = (row["content_hash"], _date_str(row["publish_date"]))

        local_path = local_paths.get(slug)
        draft = None
        parse_failed = False
        if local_path is not None:
            try:
                draft = parse_draft(local_path.read_text(encoding="utf-8"))
                draft.path = local_path
            except DraftError:
                parse_failed = True

        if parse_failed:
            # Content unknown: never auto-updated. "Server moved" also
            # covers the no-base case — with nothing to compare against,
            # the relationship is unknown, which is a conflict, not a
            # quiet edit.
            server_moved = base is None or server_tuple != base
            action = Action.CONFLICT if server_moved else Action.EDITED
        else:
            local_tuple = None
            if draft is not None:
                local_tuple = (content_hash(draft.title, draft.body), _date_str(draft.date))
            action = classify(local=local_tuple, base=base, server=server_tuple)

        if action == Action.NEW_ON_SERVER:
            new_path = drafts_root / f"{slug}.md"
            new_draft = Draft(
                title=row["title"], slug=slug, date=row["publish_date"],
                body=row["content_markdown"], path=new_path,
            )
            new_path.write_text(serialize_draft(new_draft), encoding="utf-8")
            state[slug] = _base_from_row(row, "clean")
            report.new.append(slug)

        elif action == Action.AUTO_UPDATE:
            if open_slug == slug and not open_clean:
                if base_entry is not None:
                    base_entry["state"] = "conflict"
                report.conflicts.append(slug)
            else:
                updated_draft = Draft(
                    title=row["title"], slug=slug, date=row["publish_date"],
                    body=row["content_markdown"], path=local_path,
                )
                local_path.write_text(serialize_draft(updated_draft), encoding="utf-8")
                state[slug] = _base_from_row(row, "clean")
                report.updated.append(slug)

        elif action == Action.ADVANCE_BASE:
            # No body change — but canonicalize the date: header to the
            # server's string (and, incidentally, the header formatting)
            # by re-serializing the already-parsed draft.
            draft.date = row["publish_date"]
            local_path.write_text(serialize_draft(draft), encoding="utf-8")
            state[slug] = _base_from_row(row, "clean")
            report.adopted.append(slug)

        elif action == Action.CONFLICT:
            if base_entry is not None:
                base_entry["state"] = "conflict"
            report.conflicts.append(slug)

        elif action == Action.EDITED:
            if base_entry is not None:
                base_entry["state"] = "edited"

        elif action == Action.CLEAN:
            if base_entry is not None:
                base_entry["state"] = "clean"

        old_assets_hash = base_entry.get("assets_hash") if base_entry is not None else None
        _sync_assets(client, drafts_root, slug, row, old_assets_hash, report, pace)

    save_state(state_path, state)
    return report


def _date_str(value: str | None) -> str:
    """`None` compares as `""`, consistently on every side of a comparison."""
    return value if value is not None else ""


def _base_tuple(entry: dict | None) -> tuple[str, str] | None:
    if entry is None:
        return None
    return (entry.get("hash", ""), _date_str(entry.get("date")))


def _base_from_row(row: dict, state_label: str) -> dict:
    return {
        "hash": row["content_hash"],
        "date": row["publish_date"],
        "assets_hash": row["assets_hash"],
        "state": state_label,
    }


def _sync_assets(
    client, drafts_root: Path, slug: str, row: dict,
    old_assets_hash: str | None, report: SyncReport, pace: float,
) -> None:
    """Reconcile one slug's `<slug>.assets/` against the server's list.

    Only runs when the server's current `assets_hash` differs from the
    recorded base (or there is no base yet — `old_assets_hash is None`).
    A file absent locally is downloaded; a file already present, whether
    it matches the server's copy or not, is left untouched. Nothing is
    ever deleted.
    """
    new_assets_hash = row["assets_hash"]
    if old_assets_hash is not None and old_assets_hash == new_assets_hash:
        return

    try:
        assets = client.list_assets(slug)
    except ClientError as exc:
        report.errors.append(str(exc))
        return

    assets_dir = drafts_root / f"{slug}.assets"
    for item in assets:
        name = item["name"]
        local_file = assets_dir / name
        if local_file.exists():
            continue
        try:
            data = client.download_asset(slug, name)
        except ClientError as exc:
            report.errors.append(str(exc))
            continue
        assets_dir.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(data)
        report.assets_downloaded += 1
        time.sleep(pace)
