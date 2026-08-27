import hashlib
import json
import time
from pathlib import Path

import pytest

from writer.client import ClientError
from writer.draft import Draft, serialize_draft
from writer.sync import (
    Action,
    classify,
    content_hash,
    load_state,
    run_sync,
    save_state,
)

H1, H2, H3 = "aaa", "bbb", "ccc"
D1, D2 = "2023-01-01T00:00:00+00:00", "2024-02-02T00:00:00+00:00"


def test_content_hash_matches_pinned_vector():
    assert content_hash("t", "b") == (
        "ef7d21565aeb99d3811b83445ef927ed4cc9efd7267b1695120822c4c5333dfe")

def test_clean_when_all_three_agree():
    assert classify(local=(H1, D1), base=(H1, D1), server=(H1, D1)) == Action.CLEAN

def test_local_change_only_is_edited():
    assert classify(local=(H2, D1), base=(H1, D1), server=(H1, D1)) == Action.EDITED

def test_local_date_change_only_is_edited():
    assert classify(local=(H1, D2), base=(H1, D1), server=(H1, D1)) == Action.EDITED

def test_server_change_only_is_auto_update():
    assert classify(local=(H1, D1), base=(H1, D1), server=(H2, D1)) == Action.AUTO_UPDATE

def test_server_date_change_only_is_auto_update():
    assert classify(local=(H1, D1), base=(H1, D1), server=(H1, D2)) == Action.AUTO_UPDATE

def test_both_changed_identically_advances_base():
    assert classify(local=(H2, D2), base=(H1, D1), server=(H2, D2)) == Action.ADVANCE_BASE

def test_both_changed_differently_is_conflict():
    assert classify(local=(H2, D1), base=(H1, D1), server=(H3, D1)) == Action.CONFLICT

def test_no_server_entry_is_local_only():
    assert classify(local=(H1, D1), base=(H1, D1), server=None) == Action.LOCAL_ONLY

def test_no_server_entry_and_no_base_is_local_only():
    assert classify(local=(H1, D1), base=None, server=None) == Action.LOCAL_ONLY

def test_no_local_file_is_new_on_server():
    assert classify(local=None, base=None, server=(H1, D1)) == Action.NEW_ON_SERVER

def test_no_local_file_with_stale_base_is_new_on_server():
    assert classify(local=None, base=(H1, D1), server=(H2, D1)) == Action.NEW_ON_SERVER

def test_no_base_local_matches_server_adopts_clean():
    assert classify(local=(H1, D1), base=None, server=(H1, D1)) == Action.ADVANCE_BASE

def test_no_base_local_differs_from_server_is_conflict():
    assert classify(local=(H2, D1), base=None, server=(H1, D1)) == Action.CONFLICT


def test_missing_state_file_is_empty(tmp_path):
    assert load_state(tmp_path / "absent.json") == {}

def test_corrupt_state_file_is_empty(tmp_path):
    p = tmp_path / ".sync.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_state(p) == {}

def test_state_file_with_a_non_dict_entry_drops_only_that_entry(tmp_path):
    """A slug mapped to something that isn't an entry is no entry at all."""
    p = tmp_path / ".sync.json"
    good = {"hash": H1, "date": D1, "state": "clean"}
    p.write_text(
        json.dumps({"good": good, "bad": "clean", "worse": ["clean"], "worst": None}),
        encoding="utf-8",
    )
    assert load_state(p) == {"good": good}


def test_state_round_trips(tmp_path):
    p = tmp_path / ".sync.json"
    state = {"s": {"hash": H1, "date": D1, "assets_hash": H2, "state": "clean"}}
    save_state(p, state)
    assert load_state(p) == state
    assert json.loads(p.read_text(encoding="utf-8")) == state


# --- The sync engine (Task 5) ------------------------------------------

def _assets_hash(files: dict) -> str:
    """The server's real recipe: sha256 over sorted `name:sha256` lines."""
    lines = sorted(f"{name}:{hashlib.sha256(data).hexdigest()}" for name, data in files.items())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


class FakeSyncClient:
    """Models server state for engine tests. Importable by later tasks' tests.

    `entries`: slug -> {"title", "content_markdown", "publish_date"}.
    `assets`: slug -> {name -> bytes}. `content_hash`/`assets_hash` are
    derived fresh on every call, with the same recipes the real server
    (and `writer.sync.content_hash`) use, so a stale-hash bug in the
    engine would surface here rather than being hidden by a canned
    fixture.

    `fail=True` makes every method raise `ClientError` (models a
    downed/unreachable server). `fail_assets_for` raises `ClientError`
    only from `list_assets`/`download_asset`, only for the named slugs
    (models one entry's asset endpoint failing mid-sync).
    """

    def __init__(self, entries=None, assets=None, fail=False, fail_assets_for=None):
        self.entries = entries if entries is not None else {}
        self.assets = assets if assets is not None else {}
        self.fail = fail
        self.fail_assets_for = fail_assets_for if fail_assets_for is not None else set()
        self.calls = {"list_entries_full": 0, "list_assets": 0, "download_asset": 0}
        self.downloaded_log = []

    def list_entries_full(self) -> list:
        self.calls["list_entries_full"] += 1
        if self.fail:
            raise ClientError("offline")
        rows = []
        for slug, e in self.entries.items():
            rows.append({
                "slug": slug,
                "title": e["title"],
                "publish_date": e["publish_date"],
                "content_hash": content_hash(e["title"], e["content_markdown"]),
                "assets_hash": _assets_hash(self.assets.get(slug, {})),
                "content_markdown": e["content_markdown"],
            })
        return rows

    def list_assets(self, slug: str) -> list:
        self.calls["list_assets"] += 1
        if self.fail or slug in self.fail_assets_for:
            raise ClientError("offline")
        return [
            {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(self.assets.get(slug, {}).items())
        ]

    def download_asset(self, slug: str, name: str) -> bytes:
        self.calls["download_asset"] += 1
        if self.fail or slug in self.fail_assets_for:
            raise ClientError("offline")
        self.downloaded_log.append((slug, name))
        return self.assets[slug][name]


def _write_draft(drafts_root: Path, title: str, slug: str, body: str, date=None, extra=None) -> Path:
    path = drafts_root / f"{slug}.md"
    d = Draft(title=title, slug=slug, date=date, body=body, path=path, extra=extra or {})
    path.write_text(serialize_draft(d), encoding="utf-8")
    return path


def _sidecar(tmp_path: Path) -> dict:
    return load_state(tmp_path / ".sync.json")


# Rule: list_entries_full called once, list state loaded once.

def test_list_entries_full_called_exactly_once(tmp_path):
    client = FakeSyncClient(entries={
        "a": {"title": "A", "content_markdown": "body a", "publish_date": D1},
    })
    run_sync(client, tmp_path, pace=0)
    assert client.calls["list_entries_full"] == 1


def test_load_state_called_exactly_once(tmp_path, monkeypatch):
    import writer.sync as sync_mod

    calls = []
    real_load_state = sync_mod.load_state

    def spy(path):
        calls.append(path)
        return real_load_state(path)

    monkeypatch.setattr(sync_mod, "load_state", spy)
    client = FakeSyncClient(entries={
        "a": {"title": "A", "content_markdown": "body a", "publish_date": D1},
    })
    run_sync(client, tmp_path, pace=0)
    assert len(calls) == 1


# Rule 2: NEW_ON_SERVER writes the file, records base, syncs assets.

def test_new_on_server_writes_file_and_base_and_assets(tmp_path):
    client = FakeSyncClient(
        entries={"new-slug": {"title": "New", "content_markdown": "fresh body", "publish_date": D1}},
        assets={"new-slug": {"pic.png": b"pixels"}},
    )
    report = run_sync(client, tmp_path, pace=0)

    md = tmp_path / "new-slug.md"
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "title: New" in text
    assert "fresh body" in text

    state = _sidecar(tmp_path)
    row_hash = content_hash("New", "fresh body")
    assert state["new-slug"]["hash"] == row_hash
    assert state["new-slug"]["date"] == D1
    assert state["new-slug"]["state"] == "clean"

    assert report.new == ["new-slug"]
    assert (tmp_path / "new-slug.assets" / "pic.png").read_bytes() == b"pixels"
    assert report.assets_downloaded == 1


# Rule 3: AUTO_UPDATE (unguarded) overwrites the file and advances base.

def test_auto_update_unguarded_overwrites_file(tmp_path):
    _write_draft(tmp_path, "Old Title", "s", "old body", date=D1)
    state_path = tmp_path / ".sync.json"
    old_hash = content_hash("Old Title", "old body")
    save_state(state_path, {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "New Title", "content_markdown": "new body", "publish_date": D2},
    })
    report = run_sync(client, tmp_path, pace=0)

    text = (tmp_path / "s.md").read_text(encoding="utf-8")
    assert "title: New Title" in text
    assert "new body" in text

    state = _sidecar(tmp_path)
    assert state["s"]["hash"] == content_hash("New Title", "new body")
    assert state["s"]["date"] == D2
    assert state["s"]["state"] == "clean"
    assert report.updated == ["s"]
    assert report.conflicts == []


# Rule 3: AUTO_UPDATE guarded by the open-editor guard becomes a conflict.

def test_auto_update_guarded_by_open_editor_marks_conflict(tmp_path):
    path = _write_draft(tmp_path, "Old Title", "s", "old body", date=D1)
    before = path.read_text(encoding="utf-8")
    old_hash = content_hash("Old Title", "old body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "New Title", "content_markdown": "new body", "publish_date": D2},
    })
    report = run_sync(client, tmp_path, open_slug="s", open_clean=False, pace=0)

    assert path.read_text(encoding="utf-8") == before
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "conflict"
    # base is left untouched — only the state label flips
    assert state["s"]["hash"] == old_hash
    assert state["s"]["date"] == D1
    assert report.conflicts == ["s"]
    assert report.updated == []


def test_auto_update_open_but_clean_still_updates(tmp_path):
    # Pass-through side of the guard: the slug is open in the editor,
    # but the editor is clean, so AUTO_UPDATE proceeds normally.
    path = _write_draft(tmp_path, "Old Title", "s", "old body", date=D1)
    old_hash = content_hash("Old Title", "old body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "New Title", "content_markdown": "new body", "publish_date": D2},
    })
    report = run_sync(client, tmp_path, open_slug="s", open_clean=True, pace=0)

    text = path.read_text(encoding="utf-8")
    assert "title: New Title" in text
    assert "new body" in text
    assert report.updated == ["s"]
    assert report.conflicts == []


# Rule 4: ADVANCE_BASE — no body change, but the date header is canonicalized
# and the base advances. Exercised two ways: the no-base "adopt" degraded
# rule, and the both-changed-identically case.

def test_advance_base_adopts_preexisting_draft_with_no_sidecar(tmp_path):
    _write_draft(tmp_path, "Match", "s", "matching body", date=D1)
    # no prior .sync.json at all

    client = FakeSyncClient(entries={
        "s": {"title": "Match", "content_markdown": "matching body", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, pace=0)

    state = _sidecar(tmp_path)
    assert state["s"]["hash"] == content_hash("Match", "matching body")
    assert state["s"]["date"] == D1
    assert state["s"]["state"] == "clean"
    assert report.adopted == ["s"]


def test_advance_base_canonicalizes_date_header_when_both_changed_identically(tmp_path):
    old_hash = content_hash("Old", "old body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})

    # Hand-write the local file with extra header whitespace — same
    # parsed fields as the server's, but not byte-identical.
    path = tmp_path / "s.md"
    path.write_text("title:  New Both  \nslug: s\ndate:   " + D2 + "  \n\nnew body", encoding="utf-8")

    client = FakeSyncClient(entries={
        "s": {"title": "New Both", "content_markdown": "new body", "publish_date": D2},
    })
    report = run_sync(client, tmp_path, pace=0)

    text = path.read_text(encoding="utf-8")
    assert text == f"title: New Both\nslug: s\ndate: {D2}\n\nnew body"

    state = _sidecar(tmp_path)
    assert state["s"]["hash"] == content_hash("New Both", "new body")
    assert state["s"]["date"] == D2
    assert state["s"]["state"] == "clean"
    assert report.adopted == ["s"]


# Rule 5: CONFLICT marks the sidecar and never touches the file.

def test_conflict_when_both_changed_differently(tmp_path):
    old_hash = content_hash("Old", "old body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})
    path = _write_draft(tmp_path, "Local Edit", "s", "local body", date=D1)
    before = path.read_text(encoding="utf-8")

    client = FakeSyncClient(entries={
        "s": {"title": "Server Edit", "content_markdown": "server body", "publish_date": D2},
    })
    report = run_sync(client, tmp_path, pace=0)

    assert path.read_text(encoding="utf-8") == before
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "conflict"
    # the base itself is left exactly as it was — only "state" flips
    assert state["s"]["hash"] == old_hash
    assert state["s"]["date"] == D1
    assert report.conflicts == ["s"]


def test_conflict_with_no_base_never_touches_file(tmp_path):
    path = _write_draft(tmp_path, "Local", "s", "local body", date=D1)
    before = path.read_text(encoding="utf-8")
    # no prior .sync.json

    client = FakeSyncClient(entries={
        "s": {"title": "Server", "content_markdown": "server body", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, pace=0)

    assert path.read_text(encoding="utf-8") == before
    assert report.conflicts == ["s"]

    # The mark is recorded, not merely reported: a ⚠ that lived only in
    # the report would be gone by the next restart, and with it the fact
    # that the server has this slug at all. State only — claiming a hash
    # or a date here would claim a sync point that never happened.
    state = _sidecar(tmp_path)
    assert state["s"] == {"state": "conflict", "assets_hash": _assets_hash({})}


def test_state_only_conflict_entry_classifies_as_if_there_were_no_base(tmp_path):
    """The three ways out of a recorded no-base conflict, all unchanged.

    `_base_tuple` reads a state-only entry as `("", "")`, which no real
    local or server tuple can equal — so the next sync reaches the same
    verdict it would have with no sidecar entry at all.
    """
    server = {"title": "Server", "content_markdown": "server body", "publish_date": D1}
    client = FakeSyncClient(entries={"s": dict(server)})

    # 1. Still differing -> still a conflict, file still untouched.
    path = _write_draft(tmp_path, "Local", "s", "local body", date=D1)
    run_sync(client, tmp_path, pace=0)
    before = path.read_text(encoding="utf-8")
    report = run_sync(client, tmp_path, pace=0)
    assert report.conflicts == ["s"]
    assert path.read_text(encoding="utf-8") == before
    assert _sidecar(tmp_path)["s"]["state"] == "conflict"

    # 2. Resolved to the server's own content -> adopted as clean.
    _write_draft(tmp_path, "Server", "s", "server body", date=D1)
    report = run_sync(client, tmp_path, pace=0)
    assert report.adopted == ["s"]
    state = _sidecar(tmp_path)
    assert state["s"]["hash"] == content_hash("Server", "server body")
    assert state["s"]["state"] == "clean"

    # 3. And from a recorded conflict with the file removed -> new on server.
    save_state(tmp_path / ".sync.json", {"s": {"state": "conflict"}})
    (tmp_path / "s.md").unlink()
    report = run_sync(client, tmp_path, pace=0)
    assert report.new == ["s"]
    assert (tmp_path / "s.md").exists()
    assert _sidecar(tmp_path)["s"]["state"] == "clean"


# Rule 5b: the open-editor guard covers the ADVANCE_BASE rewrite too.

def test_advance_base_guarded_by_open_editor_skips_the_rewrite(tmp_path):
    """A canonicalizing rewrite is still a rewrite, and the app reloads
    what a sync writes — so an open, unclean editor stops it, exactly as
    it stops an auto-update. The base advances anyway: local already
    equals the server, so it is true of the file either way, and only
    the header's layout waits.
    """
    path = tmp_path / "s.md"
    spaced = "title:  Match  \nslug: s\ndate:   " + D1 + "  \n\nmatching body"
    path.write_text(spaced, encoding="utf-8")

    client = FakeSyncClient(entries={
        "s": {"title": "Match", "content_markdown": "matching body", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, open_slug="s", open_clean=False, pace=0)

    assert path.read_text(encoding="utf-8") == spaced
    assert report.adopted == []  # `adopted` names the files this sync rewrote
    state = _sidecar(tmp_path)
    assert state["s"]["hash"] == content_hash("Match", "matching body")
    assert state["s"]["date"] == D1
    assert state["s"]["state"] == "clean"


def test_advance_base_open_but_clean_still_canonicalizes(tmp_path):
    # Pass-through side of the guard, as for AUTO_UPDATE: the slug is
    # open, but the editor agrees with the file, so the rewrite proceeds.
    path = tmp_path / "s.md"
    path.write_text(
        "title:  Match  \nslug: s\ndate:   " + D1 + "  \n\nmatching body", encoding="utf-8"
    )

    client = FakeSyncClient(entries={
        "s": {"title": "Match", "content_markdown": "matching body", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, open_slug="s", open_clean=True, pace=0)

    assert path.read_text(encoding="utf-8") == f"title: Match\nslug: s\ndate: {D1}\n\nmatching body"
    assert report.adopted == ["s"]


# Rule 6: EDITED / CLEAN only flip the sidecar's state label; LOCAL_ONLY
# drops a stale base entry and leaves the file alone.

def test_edited_only_updates_sidecar_state(tmp_path):
    old_hash = content_hash("Same", "same body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"}})
    path = _write_draft(tmp_path, "Changed", "s", "changed body", date=D1)
    before_mtime = path.stat().st_mtime_ns

    client = FakeSyncClient(entries={
        "s": {"title": "Same", "content_markdown": "same body", "publish_date": D1},
    })
    run_sync(client, tmp_path, pace=0)

    assert path.stat().st_mtime_ns == before_mtime
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "edited"
    assert state["s"]["hash"] == old_hash
    assert state["s"]["date"] == D1


def test_clean_only_updates_sidecar_state(tmp_path):
    h = content_hash("Same", "same body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": h, "date": D1, "assets_hash": _assets_hash({}), "state": "edited"}})
    path = _write_draft(tmp_path, "Same", "s", "same body", date=D1)
    before_mtime = path.stat().st_mtime_ns

    client = FakeSyncClient(entries={
        "s": {"title": "Same", "content_markdown": "same body", "publish_date": D1},
    })
    run_sync(client, tmp_path, pace=0)

    assert path.stat().st_mtime_ns == before_mtime
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "clean"


def test_local_only_drops_stale_sidecar_entry(tmp_path):
    # A slug that used to be mirrored, but the server row is gone now.
    save_state(tmp_path / ".sync.json", {"gone": {"hash": H1, "date": D1, "assets_hash": H2, "state": "clean"}})
    path = _write_draft(tmp_path, "Still Here", "gone", "still here body", date=D1)
    before = path.read_text(encoding="utf-8")

    client = FakeSyncClient(entries={})
    run_sync(client, tmp_path, pace=0)

    assert path.read_text(encoding="utf-8") == before
    state = _sidecar(tmp_path)
    assert "gone" not in state


def test_local_only_never_published_gets_no_sidecar_entry(tmp_path):
    _write_draft(tmp_path, "Draft Only", "unpublished", "draft body", date=None)

    client = FakeSyncClient(entries={})
    run_sync(client, tmp_path, pace=0)

    state = _sidecar(tmp_path)
    assert "unpublished" not in state


# Rule 7: asset reconciliation only for slugs whose assets_hash moved (or
# is new); downloads absent files, leaves present ones alone either way.

def test_assets_absent_locally_are_downloaded(tmp_path):
    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"one.png": b"111", "two.png": b"222"}},
    )
    report = run_sync(client, tmp_path, pace=0)

    assert (tmp_path / "s.assets" / "one.png").read_bytes() == b"111"
    assert (tmp_path / "s.assets" / "two.png").read_bytes() == b"222"
    assert report.assets_downloaded == 2
    assert set(client.downloaded_log) == {("s", "one.png"), ("s", "two.png")}


def test_assets_present_and_matching_are_skipped_not_redownloaded(tmp_path):
    assets_dir = tmp_path / "s.assets"
    assets_dir.mkdir()
    (assets_dir / "one.png").write_bytes(b"111")

    old_assets_hash = _assets_hash({"one.png": b"111"})
    old_hash = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": old_assets_hash, "state": "clean"}})
    _write_draft(tmp_path, "T", "s", "b", date=D1)

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"one.png": b"111", "two.png": b"222"}},  # a new asset added server-side
    )
    report = run_sync(client, tmp_path, pace=0)

    assert (assets_dir / "one.png").read_bytes() == b"111"
    assert (assets_dir / "two.png").read_bytes() == b"222"
    assert report.assets_downloaded == 1
    assert client.downloaded_log == [("s", "two.png")]


def test_assets_present_and_different_are_left_alone(tmp_path):
    assets_dir = tmp_path / "s.assets"
    assets_dir.mkdir()
    (assets_dir / "one.png").write_bytes(b"local-version")

    old_hash = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": "stale", "state": "clean"}})
    _write_draft(tmp_path, "T", "s", "b", date=D1)

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"one.png": b"server-version"}},
    )
    run_sync(client, tmp_path, pace=0)

    assert (assets_dir / "one.png").read_bytes() == b"local-version"
    assert client.downloaded_log == []


def test_reconciliation_never_deletes_local_only_asset(tmp_path):
    assets_dir = tmp_path / "s.assets"
    assets_dir.mkdir()
    (assets_dir / "orphan.png").write_bytes(b"only-local")

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"server.png": b"server-data"}},  # server has never heard of orphan.png
    )
    run_sync(client, tmp_path, pace=0)

    assert (assets_dir / "orphan.png").read_bytes() == b"only-local"
    assert (assets_dir / "server.png").read_bytes() == b"server-data"


class _CorruptDownloadClient(FakeSyncClient):
    """`download_asset` returns bytes that don't match the sha256 it
    advertised via `list_assets` — models a corrupted/truncated transfer.
    """

    def download_asset(self, slug: str, name: str) -> bytes:
        return super().download_asset(slug, name) + b"\x00tampered"


def test_downloaded_asset_checksum_mismatch_is_not_written(tmp_path):
    client = _CorruptDownloadClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"pic.png": b"original"}},
    )
    report = run_sync(client, tmp_path, pace=0)

    assert not (tmp_path / "s.assets" / "pic.png").exists()
    assert report.assets_downloaded == 0
    assert len(report.errors) == 1
    assert report.errors[0].startswith("s:")
    assert "pic.png" in report.errors[0]


def test_unsafe_asset_name_is_rejected(tmp_path):
    client = FakeSyncClient(entries={
        "s": {"title": "T", "content_markdown": "b", "publish_date": D1},
    })
    # A misbehaving/malicious server sending a path-escaping name — the
    # constructor's own hashing never produces one of these, so it's set
    # directly to model the server response.
    client.assets = {"s": {"../escape.png": b"data"}}

    report = run_sync(client, tmp_path, pace=0)

    assert not (tmp_path.parent / "escape.png").exists()
    assert report.assets_downloaded == 0
    assert len(report.errors) == 1
    assert report.errors[0].startswith("s:")
    assert "escape.png" in report.errors[0]


def test_assets_unchanged_from_base_skips_list_assets_call(tmp_path):
    ah = _assets_hash({"one.png": b"111"})
    h = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": h, "date": D1, "assets_hash": ah, "state": "clean"}})
    _write_draft(tmp_path, "T", "s", "b", date=D1)

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"one.png": b"111"}},
    )
    run_sync(client, tmp_path, pace=0)

    assert client.calls["list_assets"] == 0


def test_assets_reconcile_independent_of_clean_content(tmp_path):
    # Content is CLEAN (unchanged on both sides), but assets moved on the
    # server: reconciliation is a separate axis from content sync.
    h = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": h, "date": D1, "assets_hash": "stale-assets-hash", "state": "clean"}})
    _write_draft(tmp_path, "T", "s", "b", date=D1)

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"new.png": b"new"}},
    )
    report = run_sync(client, tmp_path, pace=0)

    assert (tmp_path / "s.assets" / "new.png").read_bytes() == b"new"
    assert report.assets_downloaded == 1


def test_asset_download_error_is_recorded_and_sync_continues(tmp_path):
    save_state(tmp_path / ".sync.json", {})
    client = FakeSyncClient(
        entries={
            "broken": {"title": "B", "content_markdown": "bb", "publish_date": D1},
            "fine": {"title": "F", "content_markdown": "ff", "publish_date": D1},
        },
        assets={
            "broken": {"pic.png": b"x"},
            "fine": {"pic.png": b"y"},
        },
        fail_assets_for={"broken"},
    )
    report = run_sync(client, tmp_path, pace=0)

    assert len(report.errors) == 1  # one one-line truth recorded for "broken"
    assert report.errors[0].startswith("broken:")  # the slug is named
    # sync kept going for the other slug
    assert (tmp_path / "fine.assets" / "pic.png").read_bytes() == b"y"
    assert report.new == ["broken", "fine"]


def test_asset_pace_sleeps_after_the_listing_and_between_downloads(tmp_path, monkeypatch):
    import writer.sync as sync_mod

    slept = []
    monkeypatch.setattr(sync_mod.time, "sleep", lambda s: slept.append(s))

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"one.png": b"1", "two.png": b"2"}},
    )
    run_sync(client, tmp_path, pace=0.5)

    # One for the listing, then one per download.
    assert slept == [0.5, 0.5, 0.5]


def test_asset_pace_sleeps_after_a_listing_with_nothing_to_download(tmp_path, monkeypatch):
    """`list_assets` is a request like any other, and a first sync makes one
    per entry before it downloads anything — unpaced, a large log's listings
    alone are enough to earn a 429 on the tail of the run.
    """
    import writer.sync as sync_mod

    slept = []
    monkeypatch.setattr(sync_mod.time, "sleep", lambda s: slept.append(s))

    client = FakeSyncClient(entries={
        "a": {"title": "A", "content_markdown": "body a", "publish_date": D1},
        "b": {"title": "B", "content_markdown": "body b", "publish_date": D1},
    })  # assets={} — every slug is listed, none has anything to fetch
    run_sync(client, tmp_path, pace=0.5)

    assert client.calls["list_assets"] == 2
    assert client.calls["download_asset"] == 0
    assert slept == [0.5, 0.5]


# assets_hash is owned by asset reconciliation itself, independent of the
# content action: it only advances on a fully successful reconciliation
# pass, and keeps its old value (so the next sync retries) on any failure.

def test_failed_asset_reconciliation_keeps_old_hash_and_retries_next_sync(tmp_path):
    old_assets_hash = _assets_hash({})  # nothing downloaded yet
    h = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": h, "date": D1, "assets_hash": old_assets_hash, "state": "clean"}})
    _write_draft(tmp_path, "T", "s", "b", date=D1)

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"pic.png": b"data"}},
        fail_assets_for={"s"},
    )
    report1 = run_sync(client, tmp_path, pace=0)

    assert report1.errors
    assert not (tmp_path / "s.assets" / "pic.png").exists()
    state = _sidecar(tmp_path)
    assert state["s"]["assets_hash"] == old_assets_hash  # unchanged — the failure didn't advance it

    # The server (or network) recovers; the next sync retries and succeeds.
    client.fail_assets_for = set()
    report2 = run_sync(client, tmp_path, pace=0)

    assert report2.errors == []
    assert (tmp_path / "s.assets" / "pic.png").read_bytes() == b"data"
    state = _sidecar(tmp_path)
    assert state["s"]["assets_hash"] == _assets_hash({"pic.png": b"data"})


def test_edited_slug_with_successful_reconciliation_advances_assets_hash(tmp_path):
    old_assets_hash = _assets_hash({})
    old_hash = content_hash("Same", "same body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": old_hash, "date": D1, "assets_hash": old_assets_hash, "state": "clean"}})
    _write_draft(tmp_path, "Changed", "s", "changed body", date=D1)  # local edit -> EDITED

    client = FakeSyncClient(
        entries={"s": {"title": "Same", "content_markdown": "same body", "publish_date": D1}},  # server unchanged
        assets={"s": {"pic.png": b"data"}},  # a new asset appeared server-side
    )
    run_sync(client, tmp_path, pace=0)

    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "edited"
    assert state["s"]["assets_hash"] == _assets_hash({"pic.png": b"data"})
    assert (tmp_path / "s.assets" / "pic.png").read_bytes() == b"data"


# Rule 8: an unparseable local .md is never auto-updated: CONFLICT if the
# server moved vs base, EDITED otherwise.

def test_unparseable_local_file_is_conflict_when_server_moved(tmp_path):
    path = tmp_path / "s.md"
    broken_text = "this has no colon at all\n\nbody"
    path.write_text(broken_text, encoding="utf-8")
    save_state(tmp_path / ".sync.json", {"s": {"hash": H1, "date": D1, "assets_hash": H2, "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "New", "content_markdown": "new body", "publish_date": D2},  # server moved
    })
    report = run_sync(client, tmp_path, pace=0)

    assert path.read_text(encoding="utf-8") == broken_text
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "conflict"
    assert report.conflicts == ["s"]


def test_unparseable_local_file_is_edited_when_server_unchanged(tmp_path):
    path = tmp_path / "s.md"
    broken_text = "this has no colon at all\n\nbody"
    path.write_text(broken_text, encoding="utf-8")
    server_hash = content_hash("Same", "same body")
    save_state(tmp_path / ".sync.json", {"s": {"hash": server_hash, "date": D1, "assets_hash": H2, "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "Same", "content_markdown": "same body", "publish_date": D1},  # matches base
    })
    run_sync(client, tmp_path, pace=0)

    assert path.read_text(encoding="utf-8") == broken_text
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "edited"


def test_unreadable_local_file_does_not_abort_whole_sync(tmp_path):
    # Not valid UTF-8 (e.g. a binary file saved as `<slug>.md` by mistake)
    # — `read_text` raises `UnicodeDecodeError`, not `DraftError`, and it
    # must not take the rest of the sync down with it.
    (tmp_path / "broken.md").write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \x80\x81")

    client = FakeSyncClient(entries={
        "broken": {"title": "B", "content_markdown": "bb", "publish_date": D1},
        "fine": {"title": "F", "content_markdown": "ff", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, pace=0)

    assert report.new == ["fine"]
    assert report.conflicts == ["broken"]  # content unknown, no base -> conflict
    assert (tmp_path / "fine.md").exists()
    state = _sidecar(tmp_path)
    assert state["fine"]["state"] == "clean"


def test_a_corrupt_sidecar_entry_does_not_take_the_whole_sync_down(tmp_path):
    """Rule 10 again, one level in: a bad *entry*, not a bad file.

    A slug mapped to a string reaches `_base_tuple` and `base_entry
    ["state"]` as if it were an entry, and the `AttributeError`/
    `TypeError` that follows surfaces in the app as a permanent
    "(offline)" — the one thing a corrupt sidecar must never cost.
    """
    _write_draft(tmp_path, "T", "s", "b", date=D1)
    (tmp_path / ".sync.json").write_text(
        json.dumps({"s": "clean", "other": ["not", "an", "entry"]}), encoding="utf-8"
    )

    client = FakeSyncClient(entries={
        "s": {"title": "T", "content_markdown": "b", "publish_date": D1},
    })
    report = run_sync(client, tmp_path, pace=0)

    # With no usable base, `s` is classified as if this were a first run.
    assert report.adopted == ["s"]
    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "clean"
    assert state["s"]["hash"] == content_hash("T", "b")


# Failure rule 10's other half on the write side: one target that cannot be
# written is one slug's error line, not the end of the run — and never a
# reason to skip `save_state` and lose every other slug's work with it.

def test_an_unwritable_draft_is_recorded_and_the_rest_of_the_sync_continues(tmp_path):
    path = _write_draft(tmp_path, "Old Title", "locked", "old body", date=D1)
    before = path.read_text(encoding="utf-8")
    old_hash = content_hash("Old Title", "old body")
    save_state(tmp_path / ".sync.json", {
        "locked": {"hash": old_hash, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"},
    })
    path.chmod(0o444)

    client = FakeSyncClient(entries={
        "locked": {"title": "New Title", "content_markdown": "new body", "publish_date": D2},
        "fine": {"title": "F", "content_markdown": "ff", "publish_date": D1},
    })
    try:
        report = run_sync(client, tmp_path, pace=0)
    finally:
        path.chmod(0o644)

    assert len(report.errors) == 1
    assert report.errors[0].startswith("locked:")  # the slug is named
    assert report.updated == []
    assert path.read_text(encoding="utf-8") == before

    state = _sidecar(tmp_path)
    # The base did not advance, so the next sync retries the same update.
    assert state["locked"]["hash"] == old_hash
    assert state["locked"]["date"] == D1
    # And save_state still ran, carrying the other slug's work with it.
    assert (tmp_path / "fine.md").exists()
    assert state["fine"]["state"] == "clean"


def test_an_unwritable_assets_dir_is_recorded_and_state_is_still_saved(tmp_path):
    _write_draft(tmp_path, "T", "s", "b", date=D1)
    h = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {
        "s": {"hash": h, "date": D1, "assets_hash": _assets_hash({}), "state": "clean"},
    })
    locked = tmp_path / "s.assets"
    locked.mkdir()
    locked.chmod(0o555)  # exists, but nothing new can be created inside it

    client = FakeSyncClient(
        entries={"s": {"title": "T", "content_markdown": "b", "publish_date": D1}},
        assets={"s": {"pic.png": b"data"}},
    )
    try:
        report = run_sync(client, tmp_path, pace=0)
    finally:
        locked.chmod(0o755)

    assert len(report.errors) == 1
    assert report.errors[0].startswith("s:")
    assert report.assets_downloaded == 0
    # A failed pass keeps the old assets_hash, so the next sync retries.
    state = _sidecar(tmp_path)
    assert state["s"]["assets_hash"] == _assets_hash({})


# The None-date convention: a local draft with no date: header compares
# as "" consistently against base and server.

def test_missing_local_date_compares_as_empty_string(tmp_path):
    _write_draft(tmp_path, "T", "s", "b", date=None)
    h = content_hash("T", "b")
    save_state(tmp_path / ".sync.json", {"s": {"hash": h, "date": "", "assets_hash": _assets_hash({}), "state": "clean"}})

    client = FakeSyncClient(entries={
        "s": {"title": "T", "content_markdown": "b", "publish_date": ""},
    })
    run_sync(client, tmp_path, pace=0)

    state = _sidecar(tmp_path)
    assert state["s"]["state"] == "clean"


# Rule 9: save_state is called exactly once, at the end.

def test_save_state_called_exactly_once(tmp_path, monkeypatch):
    import writer.sync as sync_mod

    calls = []
    real_save_state = sync_mod.save_state

    def spy(path, state):
        calls.append((path, state))
        return real_save_state(path, state)

    monkeypatch.setattr(sync_mod, "save_state", spy)
    client = FakeSyncClient(entries={
        "a": {"title": "A", "content_markdown": "body a", "publish_date": D1},
        "b": {"title": "B", "content_markdown": "body b", "publish_date": D1},
    })
    run_sync(client, tmp_path, pace=0)
    assert len(calls) == 1


# Extras: a full first sync populates everything; a quiet second sync
# rewrites nothing; list_entries_full's ClientError propagates.

def _three_entry_client():
    return FakeSyncClient(entries={
        "one": {"title": "One", "content_markdown": "body one", "publish_date": D1},
        "two": {"title": "Two", "content_markdown": "body two", "publish_date": D1},
        "three": {"title": "Three", "content_markdown": "body three", "publish_date": D1},
    })


def test_full_first_sync_populates_files_and_sidecar(tmp_path):
    client = _three_entry_client()
    report = run_sync(client, tmp_path, pace=0)

    assert sorted(report.new) == ["one", "three", "two"]
    for slug in ("one", "two", "three"):
        assert (tmp_path / f"{slug}.md").exists()
    state = _sidecar(tmp_path)
    assert set(state) == {"one", "two", "three"}
    assert all(entry["state"] == "clean" for entry in state.values())


def test_quiet_second_sync_rewrites_nothing(tmp_path):
    client = _three_entry_client()
    run_sync(client, tmp_path, pace=0)

    paths = [tmp_path / f"{slug}.md" for slug in ("one", "two", "three")]
    mtimes_before = [p.stat().st_mtime_ns for p in paths]
    list_assets_calls_before = client.calls["list_assets"]
    time.sleep(0.01)

    report = run_sync(client, tmp_path, pace=0)

    mtimes_after = [p.stat().st_mtime_ns for p in paths]
    assert mtimes_before == mtimes_after
    assert report.new == []
    assert report.updated == []
    assert report.conflicts == []
    assert report.adopted == []
    assert report.assets_downloaded == 0
    # every slug's assets_hash already matches its (now-recorded) base,
    # so the second sync shouldn't even ask about assets again
    assert client.calls["list_assets"] == list_assets_calls_before


def test_list_entries_full_client_error_propagates(tmp_path):
    client = FakeSyncClient(fail=True)
    with pytest.raises(ClientError):
        run_sync(client, tmp_path, pace=0)
