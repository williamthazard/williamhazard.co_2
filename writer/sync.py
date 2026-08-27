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
from pathlib import Path


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
