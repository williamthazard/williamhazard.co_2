# writer

A terminal app for writing and publishing entries to the site's log
(currently deployed at williamhazard-web.onrender.com while the site is
in development). The whole log is local-first: every published entry is
mirrored to a file on disk, kept in sync with the server at startup and
on `ctrl+r`, so editing anything is instant and works fully offline —
see "The mirror," below. It edits files on disk and talks to the site's
writer API (`website/api.py`, under `/api/writer/`) to preview, sync, and
publish them. The app never touches the Django project directly — it
only speaks HTTP to it.

## Setup

The app has its own virtual environment, separate from the server's
`.venv/` at the repo root:

```
python3 -m venv writer/.venv
writer/.venv/bin/pip install -r writer/requirements.txt
```

Run it from the repo root (not from inside `writer/`), since `writer` is
a package and needs the repo root on the import path:

```
writer/.venv/bin/python -m writer
```

## The `writer` command

`writer/bin/writer` is a launcher that finds the checkout and runs the app
from anywhere. Install it once by symlinking it onto `$PATH`:

```
ln -s "$(pwd)/writer/bin/writer" /opt/homebrew/bin/writer
```

Then `writer` opens the app from any directory. The checkout is resolved
in order: the checkout containing the current directory (walking up,
looking for `manage.py` + `writer/app.py`); `$LOGWRITER_ROOT`, if set;
otherwise the checkout the symlinked script lives in — so the symlink
itself records the default, with no machine-specific path stored anywhere.

```
writer root              # show the default checkout and what would run here
writer root <path>       # change the default checkout (re-points the symlink)
```

## Environment variables

- `BLOG_WRITER_TOKEN` — the bearer token sent as `Authorization: Bearer
  <token>` on every request to a non-local server. Required to publish or
  list entries against production; not required against a local `DEBUG`
  server (`BLOG_WRITER_BASE_URL` pointing at `127.0.0.1` or `localhost`).
  Never commit this value anywhere — not to a config file, a shell
  script, or a commit message. It belongs in a local environment
  variable or a password manager, never in version control.
- `BLOG_WRITER_BASE_URL` — the server to talk to. Defaults to
  `https://williamhazard-web.onrender.com` (the in-development site's
  deploy — williamhazard.co still serves the old site and is never the
  target until it aliases to this deploy). Set it to
  `http://127.0.0.1:8000` to work against a local `manage.py runserver`.
- `LOG_DRAFTS_DIR` — the one flat directory holding everything local:
  unpublished drafts, the mirrored log (see "The mirror," below), every
  entry's `<slug>.assets/` directory, and the sync sidecar `.sync.json`.
  Defaults to `~/Documents/log-drafts`. Created automatically if it
  doesn't exist.

## The draft format

One file per draft, `<slug>.md`, in the drafts directory. `key: value`
header lines, a blank line, then the body verbatim:

```
title: a walk at dusk
slug: 240115-dusk
date: 2024-01-15

The road held the last of the light a little longer than the field did.
```

`title` and `slug` are required; `date` is optional. Any other key is
carried through rather than dropped or rejected — it reappears below the
known keys on save, and a warning marks it as unrecognized so a typo
stays visible instead of silently vanishing. The body is preserved
byte-for-byte: blank lines, trailing newline, and internal whitespace all
matter, since markdown is line-sensitive.

Images for a draft live beside it, in `<slug>.assets/`. `ctrl+o` adds
one from inside the app: a dialog takes the path to the image file
(dragging a file from Finder onto the terminal pastes its path) and an
optional name — blank means the file's own, and a distinctive name is
worth choosing, since published assets share one flat namespace. The
file is copied into the draft's assets folder and the markdown
reference is inserted at the cursor, ready for its alt text:

```
![](/media/log_assets/dusk-road.jpg)
```

An add whose name already exists in the folder with the same bytes is a
quiet no-op; with different bytes it refuses, so nothing is ever
silently replaced. Nothing uploads at add time — publishing remains the
only moment assets leave the machine.

## The mirror

The whole log lives locally. Every published entry is mirrored into
`LOG_DRAFTS_DIR` as an ordinary `<slug>.md` file (plus a `<slug>.assets/`
directory for its images), synced from the server at startup and again on
`ctrl+r`. Editing anything — an unpublished draft or a mirrored entry —
touches only the local file and is instant, online or off; pushing a
change back to the server happens afterward, deliberately, one entry at a
time (`ctrl+b`) or in bulk (`ctrl+f`).

The sidebar has two sections, split by a `── log ──` divider:

- **drafts**, above — local-only files the server has never seen, each
  marked `○`, newest name first (date-prefixed slugs keep fresh work at
  the top).
- **log**, below — every entry the mirror has synced, newest first, each
  marked by how it compares to what the last sync recorded:
  - unmarked — agrees with the server
  - `●` — edited locally since the last sync
  - `⚠` — moved on both sides since the last sync (a conflict)

Every row, in either section, opens the same kind of local file — nothing
in this app exists only on the server.

### `.sync.json`

Sync keeps its own record, `.sync.json`, in the drafts directory
alongside the files it describes. For each mirrored slug it normally
holds the content hash and publish date as of the last sync, an
`assets_hash` digest of that entry's current images, and the marker
label (`clean` / `edited` / `conflict`) — with one exception: a conflict
recorded with no prior sync data to attach it to is state-only (just the
marker, no hash or date), which is enough to remember that the server
has the slug at all across a restart. It's always safe to delete: with
nothing recorded, every slug simply has no base to compare against,
which degrades sync to first-run behavior — a local file that matches
the server exactly is adopted as clean, one that doesn't becomes a
conflict. Deleting the sidecar never causes an overwrite in either
direction.

### Conflicts

Selecting a `⚠` row opens a conflict modal instead of the file directly:

- **keep mine** — keeps the local text exactly as it is and advances the
  recorded base to the server's values; the row becomes `●`.
- **take server** — rewrites the local file from the server's copy; the
  row becomes clean.
- **view diff** — a scrollable unified diff of title + body, local vs.
  server.

Every way out of the modal, including cancel, then opens the file in the
editor — editing a conflicted entry is always allowed. Publishing one is
not: `ctrl+b` refuses, before the dialog even opens, until the conflict
is resolved one of the two ways above.

### How sync behaves when the two sides disagree

- It never deletes anything. A server-side deletion just drops the
  slug's recorded base, demoting its row to `○` local-only — the local
  file, if one is still there, is left untouched.
- It never overwrites a local edit with the server's: writing a
  server-newer body over a local file requires the local file to still
  match the last-known base exactly. Anything else becomes a `⚠`
  conflict instead of a silent overwrite.
- Offline is quiet, not broken: a sync that can't reach the server, or
  can't authenticate, skips silently. Every file stays editable, and the
  markers keep showing whatever the last successful sync knew; `ctrl+r`
  retries.
- The file open in the editor right now is never rewritten out from
  under it. An update that would land on unsaved text is marked a
  conflict instead of applied.
- A local deletion isn't tracked. Removing a mirrored file by hand tells
  sync nothing — the next sync finds no local file but an unchanged
  server entry, and simply writes the file back.

### The cost of a first sync

A first sync — nothing yet recorded in `.sync.json` — mirrors every
published entry from a single request, then downloads every image those
entries have, one file at a time, with a short pace between downloads to
stay under the server's read-request limit. For an archive with a lot of
images, that's the slow part of a first run on a new machine — but it's a
one-time cost: once a base is recorded, later syncs only touch entries or
assets that actually changed.

## Keys

| Key | Action | Does |
|---|---|---|
| `ctrl+s` | save | Force a save of the current editor text (autosave already does this on every parseable keystroke; this is a manual echo of it). |
| `ctrl+r` | sync | Rebuild the sidebar from what's on disk, then run a sync in the background: fetch the entries list, mirror every new or server-changed entry into local files, reconcile assets, and update `.sync.json`. Runs at startup too; there is no separate refresh key. |
| `ctrl+t` | draft/meta | Switch focus between the `draft` body tab and the `meta` header tab. |
| `ctrl+g` | drafts | Move focus to the sidebar. |
| `ctrl+n` | new | Open the new-draft dialog (title, slug, optional date). |
| `ctrl+o` | image | Copy an image file into the current draft's assets and insert its markdown reference at the cursor. |
| `ctrl+l` | preview | Open the current draft's preview page in a browser, starting the local dev server first if it isn't already running. |
| `ctrl+b` | publish | Open the publish dialog (share-to-bluesky, share-to-mastodon) and publish the current draft. Refused, before the dialog opens, for a slug still marked `⚠`, or while a push (`ctrl+f`) is already running. |
| `ctrl+f` | push | Push every `●` mirrored entry to the server — assets, then the entry, in sidebar order. `⚠` rows are named and skipped rather than pushed; `○` local-only drafts are never included, since a first publish still goes through `ctrl+b`. Ends with one summary line (`pushed 3 · 1 conflict skipped (<slug>)`). |

The draft file on disk is the source of truth: every keystroke re-parses
the whole file, a parse failure is shown on the status line without
writing anything, and a parse success is saved immediately. There is no
separate unsaved state and no save dialog to dismiss.

## Publishing

Publishing a draft (`ctrl+b`) is refused outright, before the dialog even
opens, in two cases: the slug is a mirrored entry still marked `⚠` (see
Conflicts, above), or a push (`ctrl+f`) is already running in the
background — the two share one worker slot so the sidecar is never
written by both at once. Both refusals show on the status line rather
than opening anything.

Past those, the dialog itself checks one more precondition: against a
non-local server, `BLOG_WRITER_TOKEN` must be set. A missing token is
reported inside the dialog itself, and nothing is sent — the dialog stays
open with the reason shown.

Once that check passes, the dialog closes immediately and the rest of
the publish runs in the background. Any failure past that point — a
network error, an asset conflict, a server error — surfaces afterward as
a notification, not inside the dialog, since the dialog is already gone
by the time a network call could fail.

The order of operations depends on whether the slug is already
published:

- A **new slug** upserts the entry first, then uploads assets. The
  server's asset upload requires an existing entry to attach to, so the
  entry has to exist before any asset can be sent.
- An **already-published slug** uploads assets first, then upserts the
  entry. This keeps the live page from ever referencing an asset that
  hasn't landed yet.

A partial failure names exactly which files made it up before it failed.
Asset uploads are idempotent on the server, so re-publishing after a
partial failure is always safe — nothing already uploaded will be
duplicated or rejected on a byte-identical retry.

A failed publish never modifies the draft file: whatever went wrong, the
file on disk is exactly what it was before `ctrl+b` was pressed. A
successful publish can make one change to it: the local `date:` header is
rewritten to the server's own date string when it differs — the same
canonicalization an ordinary sync performs (see The mirror, above), done
here right away rather than waiting for the next sync to notice. A
never-before-published draft is the common case this touches, since it
usually carries no `date:` header at all until its first publish assigns
one. If the slug is open in the editor when this happens, the editor is
reloaded to pick up the new header; if the editor holds unsaved text for
that slug at that moment, the file is left untouched instead and the row
is marked `⚠`, the same guard an ordinary sync uses to avoid landing a
rewrite under work nobody has saved.

A successful publish also advances that entry's recorded sync base (hash
and date, immediately — `assets_hash` is a separate slice of the base
and stays whatever asset reconciliation last set it to), so the row reads
clean without waiting for the follow-up sync's own classification to
agree. That follow-up sync still runs regardless: it's what reconciles
`assets_hash`, and it's what moves a first-time publish's row from
drafts into the log.

## Minting a server token

The token is generated once, server-side:

```
.venv/bin/python manage.py mint_writer_token
```

This prints three lines: a one-time notice, then
`BLOG_WRITER_TOKEN=<token>`, then `WRITER_TOKEN_HASH=<hash>`. The values
are shown once and not stored anywhere by the command itself.

- `WRITER_TOKEN_HASH` goes into the server's environment — on Render,
  the `WRITER_TOKEN_HASH` environment variable for the site's service.
  The server only ever stores and compares the hash; it never sees or
  stores the raw token.
- `BLOG_WRITER_TOKEN` goes into whatever holds environment variables for
  the machine running this app — a local shell profile or a password
  manager, not a file in this repo.

Rotating the token is re-running the command and updating both sides:
the new hash on the server, the new token wherever the old one was kept.
The old token stops working as soon as the server's hash is replaced.

## Drafts live outside the repo

Draft files are never stored inside this repository. This repository is
public; a draft is unpublished writing, and a published entry's source
markdown does not belong in version control history either. `LOG_DRAFTS_DIR`
points at a directory outside the repo by default (`~/Documents/log-drafts`)
for this reason — moving it still means moving it to somewhere that isn't
tracked by this repository's git history.
