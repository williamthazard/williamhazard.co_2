# writer

A terminal app for writing and publishing entries to the site's log
(currently deployed at williamhazard-web.onrender.com while the site is
in development). It edits draft files on disk and talks to the site's
writer API (`website/api.py`, under `/api/writer/`) to preview, pull, and
publish them. The app never touches the Django project directly — it only
speaks HTTP to it.

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
- `LOG_DRAFTS_DIR` — where draft files and their asset directories live.
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

Images for a draft live beside it, in `<slug>.assets/`.

## Keys

| Key | Action | Does |
|---|---|---|
| `ctrl+s` | save | Force a save of the current editor text (autosave already does this on every parseable keystroke; this is a manual echo of it). |
| `ctrl+r` | refresh | Reload the drafts list from disk and re-fetch the published list from the server. |
| `ctrl+t` | draft/meta | Switch focus between the `draft` body tab and the `meta` header tab. |
| `ctrl+g` | drafts | Move focus to the sidebar. |
| `ctrl+n` | new | Open the new-draft dialog (title, slug, optional date). |
| `ctrl+l` | preview | Open the current draft's preview page in a browser, starting the local dev server first if it isn't already running. |
| `ctrl+f` | pull | Pull the highlighted published entry down into a new local draft file. |
| `ctrl+b` | publish | Open the publish dialog (share-to-bluesky, share-to-mastodon) and publish the current draft. |

The draft file on disk is the source of truth: every keystroke re-parses
the whole file, a parse failure is shown on the status line without
writing anything, and a parse success is saved immediately. There is no
separate unsaved state and no save dialog to dismiss.

## Publishing

Publishing a draft (`ctrl+b`) checks one precondition before anything
else happens: against a non-local server, `BLOG_WRITER_TOKEN` must be
set. A missing token is reported inside the dialog itself, and nothing
is sent — the dialog stays open with the reason shown.

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

Publishing never modifies the draft file. Success or failure, the file
on disk is exactly what it was before `ctrl+b` was pressed.

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
