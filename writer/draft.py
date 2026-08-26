"""The draft file format and drafts directory.

One file per draft, `key: value` header lines until the first blank line,
then the body verbatim. Same grammar in spirit as big highway's `.piece`
headers (and the server's own `_parse_draft_header` in `website/api.py`,
which deliberately reimplements this rather than importing it — this
package, and its dependencies, must never be a server dependency).

The body is preserved byte-for-byte: blank lines, trailing newline, and
internal whitespace all matter, since markdown is line-sensitive. Nothing
here collapses or reformats whitespace in the body.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REQUIRED_KEYS = ("title", "slug")
_KNOWN_KEYS = ("title", "slug", "date")


@dataclass
class Draft:
    title: str
    slug: str
    date: str | None
    body: str
    path: Path | None
    warnings: list[str] = field(default_factory=list)


class DraftError(Exception):
    """A draft header failed to parse. `line` is the 1-indexed line number."""

    def __init__(self, line: int, message: str):
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


def parse_draft(text: str) -> Draft:
    """Parse draft file text into a Draft.

    Grammar: `key: value` lines until the first blank line, then the body
    verbatim. `title` and `slug` are required; any other key is recorded as
    a warning on the returned Draft rather than an error.
    """
    lines = text.split("\n")
    header: dict[str, str] = {}
    warnings: list[str] = []
    body = None
    header_end_line = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            body = "\n".join(lines[i + 1:])
            header_end_line = i + 1
            break
        if ":" not in line:
            raise DraftError(i + 1, f"expected 'key: value', got {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        header[key] = value.strip()
        if key not in _KNOWN_KEYS:
            warnings.append(f"line {i + 1}: unknown key {key!r}")
    else:
        raise DraftError(len(lines) or 1, "missing blank line separating header from body")

    for required in _REQUIRED_KEYS:
        if required not in header:
            raise DraftError(header_end_line, f"missing required {required!r} field")

    return Draft(
        title=header["title"],
        slug=header["slug"],
        date=header.get("date"),
        body=body,
        path=None,
        warnings=warnings,
    )


def serialize_draft(d: Draft) -> str:
    """Render a Draft back to draft file text.

    Inverse of `parse_draft` for a canonical file: header lines (title,
    slug, then date if set), a blank line, then the body verbatim.
    """
    lines = [f"title: {d.title}", f"slug: {d.slug}"]
    if d.date is not None:
        lines.append(f"date: {d.date}")
    return "\n".join(lines) + "\n\n" + d.body


def drafts_dir() -> Path:
    """The local drafts directory: env `LOG_DRAFTS_DIR`, else `~/Documents/log-drafts`.

    Created if it doesn't already exist.
    """
    raw = os.environ.get("LOG_DRAFTS_DIR", "~/Documents/log-drafts")
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_drafts() -> list[Path]:
    """Every draft file (`*.md`) in the drafts directory, sorted by name."""
    return sorted(drafts_dir().glob("*.md"))


def load_draft(path: os.PathLike | str) -> Draft:
    """Read and parse a draft file, setting `Draft.path` to it."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    d = parse_draft(text)
    d.path = path
    return d


def save_draft(d: Draft) -> Draft:
    """Write a Draft to disk.

    If `d.path` is unset, it's computed from the drafts directory and the
    draft's slug and set on `d`. An already-set `d.path` is respected as-is
    (an existing draft is saved back to wherever it lives).
    """
    if d.path is None:
        d.path = drafts_dir() / f"{d.slug}.md"
    d.path.parent.mkdir(parents=True, exist_ok=True)
    d.path.write_text(serialize_draft(d), encoding="utf-8")
    return d


def assets_dir(d: Draft) -> Path:
    """Where a draft's images live: `<slug>.assets/` beside the draft file.

    Does not create the directory. If the draft hasn't been saved yet
    (`d.path` is unset), the directory is computed beside the drafts
    directory instead, matching where `save_draft` would place the file.
    """
    base = d.path.parent if d.path is not None else drafts_dir()
    return base / f"{d.slug}.assets"
