import json
from pathlib import Path

from writer.sync import Action, classify, content_hash, load_state, save_state

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

def test_state_round_trips(tmp_path):
    p = tmp_path / ".sync.json"
    state = {"s": {"hash": H1, "date": D1, "assets_hash": H2, "state": "clean"}}
    save_state(p, state)
    assert load_state(p) == state
    assert json.loads(p.read_text(encoding="utf-8")) == state
