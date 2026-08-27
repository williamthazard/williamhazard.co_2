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

    Corruption is dropped per slug, not per file: an entry that isn't a
    dict is no entry at all, and returning it would hand `_base_tuple`
    and `base_entry["state"]` something they can only crash on — which
    reaches the app as a permanent "(offline)", the one thing rule 10
    exists to prevent. Every well-formed entry beside it survives.
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
    return {slug: entry for slug, entry in state.items() if isinstance(entry, dict)}


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
#   `.md` file and advance the recorded base's hash and date.
# - The open-editor guard (the slug is open in the editor and the editor
#   isn't clean) covers both writes that can land on an open file: a
#   guarded AUTO_UPDATE demotes to a conflict rather than touch it, and a
#   guarded ADVANCE_BASE skips its canonicalizing rewrite while still
#   advancing the base — local already equals the server, so the base is
#   true of the file either way and only header layout is deferred.
# - CONFLICT / EDITED / CLEAN only ever flip the sidecar's "state" label;
#   they never touch hash/date or the file. The one exception is a
#   CONFLICT with no base at all, which records a state-only entry
#   ({"state": "conflict"}, no hash, no date) so that the mark — and the
#   fact that the server has the slug — survives a restart.
# - LOCAL_ONLY drops a stale sidecar entry (its base refers to a server
#   row that no longer exists) and otherwise leaves everything alone.
# - An unparseable, unreadable, or non-UTF-8 local file is treated as
#   "modified, content unknown": never auto-updated, CONFLICT if the
#   server moved since the base, EDITED otherwise.
# - A local file that cannot be *written* is likewise never fatal: the
#   three writing actions above go through `_write_draft_file`, which
#   records a slug-named error and leaves the base unadvanced, so the
#   run continues and the next sync retries that slug's same action.
#
# Asset reconciliation is a second, independent axis, and owns its own
# slice of the base — `assets_hash` — entirely separately from the
# content action above: for every slug the server still has, whenever
# its `assets_hash` differs from the recorded base (or there is no base
# yet), the asset list is fetched and any file absent locally is
# downloaded into `<slug>.assets/` (its bytes checked against the
# advertised sha256 before being written) — present files, whether
# matching or not, are always left alone, and nothing is ever deleted.
# The sidecar's `assets_hash` only ever advances to the server's current
# value when that slug's reconciliation pass finishes with no error at
# all (independent of whatever the content action just did); on any
# failure it keeps its old value, so the next sync retries.


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
    is instead recorded as a slug-prefixed one-line truth in
    `report.errors`, and that slug's assets are skipped past; the rest
    of the sync continues. An unreadable, non-UTF-8, or unparseable
    local file is likewise never fatal to the sync as a whole (see the
    module comment above for how it's classified).

    A local write that fails is held to the same discipline as a read: a
    slug-prefixed line in `report.errors`, that slug's base left
    unadvanced so the next sync retries it, and the run — and its final
    `save_state` — carrying on regardless (see `_write_draft_file`).

    `open_slug`/`open_clean` are the open-editor guard, and it holds for
    every write that could land on that file: an AUTO_UPDATE targeting
    the currently-open, not-clean editor is marked ⚠ conflict instead of
    overwriting it, and an ADVANCE_BASE keeps its base advance but skips
    the canonicalizing rewrite. `pace` is `time.sleep`'d after every
    request to the server's asset routes — each listing as well as each
    download.

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
            except (DraftError, OSError, UnicodeDecodeError):
                # A header that fails to parse, a file that can't be read
                # (permissions, a race with a delete), or one that isn't
                # valid UTF-8 (e.g. binary) — all "content unknown", none
                # of them fatal to the rest of the sync.
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
            if _write_draft_file(new_path, new_draft, slug, report):
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
                if _write_draft_file(local_path, updated_draft, slug, report):
                    state[slug] = _base_from_row(row, "clean")
                    report.updated.append(slug)

        elif action == Action.ADVANCE_BASE:
            if open_slug == slug and not open_clean:
                # The same guard AUTO_UPDATE gets, for the same reason:
                # this slug is open with unsaved text in front of it, and
                # a canonicalizing rewrite is still a rewrite — the app
                # reloads what a sync wrote, which would take that text
                # with it. The base still advances, because local
                # `(hash, date)` already equals the server's and that is
                # the whole of what a base records: the entry really is
                # clean, and all that is left as the poet wrote it is the
                # header's formatting. Deferring the base as well would
                # leave the slug with no record that the server has it at
                # all — the very hole the state-only conflict entry below
                # exists to close. Not reported as adopted: `adopted`
                # names the files this sync rewrote.
                state[slug] = _base_from_row(row, "clean")
            else:
                # No body change — but canonicalize the date: header to
                # the server's string (and, incidentally, the header
                # formatting) by re-serializing the already-parsed draft.
                draft.date = row["publish_date"]
                if _write_draft_file(local_path, draft, slug, report):
                    state[slug] = _base_from_row(row, "clean")
                    report.adopted.append(slug)

        elif action == Action.CONFLICT:
            if base_entry is not None:
                base_entry["state"] = "conflict"
            else:
                # No base to label — and a ⚠ that lives only in this
                # run's report is gone by the next restart, taking with
                # it the fact that the server has this slug at all. A
                # state-only entry (no hash, no date) records the mark
                # without claiming a sync point: `_base_tuple` reads it
                # as `("", "")`, which no real local or server tuple can
                # equal, so the next sync classifies the slug exactly as
                # it would with no base — CONFLICT while the two still
                # differ, ADVANCE_BASE once they agree, NEW_ON_SERVER if
                # the local file goes away.
                state[slug] = {"state": "conflict"}
            report.conflicts.append(slug)

        elif action == Action.EDITED:
            if base_entry is not None:
                base_entry["state"] = "edited"

        elif action == Action.CLEAN:
            if base_entry is not None:
                base_entry["state"] = "clean"

        old_assets_hash = base_entry.get("assets_hash") if base_entry is not None else None
        _sync_assets(client, drafts_root, slug, row, old_assets_hash, state, report, pace)

    save_state(state_path, state)
    return report


def _write_draft_file(path: Path, draft: Draft, slug: str, report: SyncReport) -> bool:
    """Serialize `draft` to `path`; `False` (with a named error) if it can't be.

    Reads are already forbidden from ending a sync — an unreadable local
    file is classified, not raised (see the module comment). A write is
    held to the same discipline: a read-only target, a full disk, or a
    directory that went missing is one slug's error line, after which
    every other slug is still processed and the sidecar is still saved.

    The caller advances that slug's base only when this returns `True`.
    A write that did not happen must not be recorded as one, or the next
    sync would read a base the file never matched; unrecorded, the same
    action is simply reached again and retried.
    """
    try:
        path.write_text(serialize_draft(draft), encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"{slug}: {exc}")
        return False
    return True


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
        "state": state_label,
    }


def _is_safe_asset_name(name: str) -> bool:
    """A server-supplied asset name must resolve to a plain file inside
    `<slug>.assets/` — never escape it via a separator or a `..` segment.
    Defense in depth, in the same spirit as the server's own basename
    guard on `serve_media`; a well-behaved server never sends one of
    these, but sync doesn't trust that blindly.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return os.path.basename(name) == name


def _sync_assets(
    client, drafts_root: Path, slug: str, row: dict,
    old_assets_hash: str | None, state: dict, report: SyncReport, pace: float,
) -> None:
    """Reconcile one slug's `<slug>.assets/` against the server's list.

    Only attempts anything when the server's current `assets_hash`
    differs from the recorded base (or there is no base yet —
    `old_assets_hash is None`). A file absent locally is downloaded and
    checked against its advertised sha256 before being written — a
    mismatch is a recorded error and the file is not written. A file
    already present, whether it matches the server's copy or not, is
    deliberately left untouched either way: sync never overwrites a
    file that's already there, so there's nothing further to decide
    once presence is established. Nothing is ever deleted.

    `state[slug]["assets_hash"]` — a separate slice of the base from
    the content hash/date, owned entirely by this function — only
    advances to the server's current value when this slug's pass
    finishes with no error at all; on any failure (or when there is no
    sidecar entry to attach it to, e.g. an unresolved conflict with no
    prior base) it is left as it was, so the next sync retries.
    """
    new_assets_hash = row["assets_hash"]
    if old_assets_hash is not None and old_assets_hash == new_assets_hash:
        if slug in state:
            state[slug]["assets_hash"] = old_assets_hash
        return

    ok = True
    try:
        assets = client.list_assets(slug)
    except ClientError as exc:
        report.errors.append(f"{slug}: {exc}")
        assets = []
        ok = False
    # Paced like a download, because it is the same kind of request: a
    # first sync of a long log makes one listing per entry before it has
    # fetched a single byte, and unpaced those alone can earn a 429 that
    # lands on the tail of the run.
    time.sleep(pace)

    assets_dir = drafts_root / f"{slug}.assets"
    for item in assets:
        name = item["name"]
        if not _is_safe_asset_name(name):
            report.errors.append(f"{slug}: refusing unsafe asset name {name!r}")
            ok = False
            continue
        local_file = assets_dir / name
        if local_file.exists():
            # Present locally already — whether it matches the server's
            # copy or differs, both cases deliberately resolve to
            # leaving the file untouched (a differing file is either a
            # pending local edit or a name collision; either way it's
            # not sync's place to overwrite it).
            continue
        try:
            data = client.download_asset(slug, name)
        except ClientError as exc:
            report.errors.append(f"{slug}: {exc}")
            ok = False
            continue
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            report.errors.append(f"{slug}: downloaded {name!r} did not match its advertised checksum")
            ok = False
            continue
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
            local_file.write_bytes(data)
        except OSError as exc:
            # Same discipline as the `.md` writes above: a target that
            # cannot be written is this slug's named error, not the end
            # of the run. `ok` stays False so `assets_hash` keeps its old
            # value and the next sync fetches this file again.
            report.errors.append(f"{slug}: {exc}")
            ok = False
            continue
        report.assets_downloaded += 1
        time.sleep(pace)

    if slug not in state:
        return
    if ok:
        state[slug]["assets_hash"] = new_assets_hash
    elif old_assets_hash is not None:
        state[slug]["assets_hash"] = old_assets_hash
