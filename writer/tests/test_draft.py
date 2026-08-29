import pytest

from writer.draft import (
    Draft,
    DraftError,
    assets_dir,
    drafts_dir,
    list_drafts,
    load_draft,
    parse_draft,
    save_draft,
    serialize_draft,
)


CANONICAL = (
    "title: bear\n"
    "slug: 230919-bear\n"
    "date: 2023-09-19 08:00\n"
    "\n"
    "The markdown body starts here.\n"
)


def test_parse_happy_path():
    d = parse_draft(CANONICAL)
    assert d.title == "bear"
    assert d.slug == "230919-bear"
    assert d.date == "2023-09-19 08:00"
    assert d.body == "The markdown body starts here.\n"
    assert d.path is None
    assert d.warnings == []


def test_parse_without_date_is_optional():
    text = "title: bear\nslug: 230919-bear\n\nsynthetic body for testing\n"
    d = parse_draft(text)
    assert d.date is None


def test_body_verbatim_round_trip_with_blank_lines_and_trailing_newline():
    text = (
        "title: bear\n"
        "slug: 230919-bear\n"
        "\n"
        "First paragraph.\n"
        "\n"
        "Second paragraph, after a blank line.\n"
        "\n"
        "\n"
        "Trailing blank lines above, and a final newline below.\n"
    )
    d = parse_draft(text)
    assert d.body == (
        "First paragraph.\n"
        "\n"
        "Second paragraph, after a blank line.\n"
        "\n"
        "\n"
        "Trailing blank lines above, and a final newline below.\n"
    )


def test_body_verbatim_no_trailing_newline():
    text = "title: bear\nslug: 230919-bear\n\nno trailing newline here"
    d = parse_draft(text)
    assert d.body == "no trailing newline here"


def test_missing_title_raises_draft_error_with_line():
    text = "slug: 230919-bear\n\nsynthetic body for testing\n"
    with pytest.raises(DraftError) as excinfo:
        parse_draft(text)
    assert excinfo.value.line == 2


def test_missing_slug_raises_draft_error_with_line():
    text = "title: bear\n\nsynthetic body for testing\n"
    with pytest.raises(DraftError) as excinfo:
        parse_draft(text)
    assert excinfo.value.line == 2


def test_missing_both_title_and_slug_raises_draft_error():
    # Header has one known key (date) but neither required field; the error
    # is reported at the blank line that closes the header, line 2.
    text = "date: 2023-09-19 08:00\n\nsynthetic body for testing\n"
    with pytest.raises(DraftError) as excinfo:
        parse_draft(text)
    assert excinfo.value.line == 2


def test_line_without_colon_raises_draft_error():
    # A malformed third line (no ':') is a distinct failure from a missing
    # separator: it's caught before any blank line is ever reached.
    text = "title: bear\nslug: 230919-bear\nno body separator here"
    with pytest.raises(DraftError):
        parse_draft(text)


def test_header_only_with_zero_trailing_newlines_raises_draft_error():
    # No blank line anywhere in the text at all: the for-loop runs to
    # completion without ever finding a separator.
    text = "title: bear\nslug: 230919-bear"
    with pytest.raises(DraftError) as excinfo:
        parse_draft(text)
    assert "missing blank line" in excinfo.value.message


def test_header_only_with_ordinary_trailing_newline_raises_draft_error():
    # str.split("\n") always appends a trailing "" when the text ends in a
    # newline. A header followed by just its own ordinary trailing newline
    # — no genuine blank-line separator, no body section — must not be
    # mistaken for a valid draft with an empty body.
    text = "title: bear\nslug: 230919-bear\n"
    with pytest.raises(DraftError) as excinfo:
        parse_draft(text)
    assert "missing blank line" in excinfo.value.message


def test_header_with_real_separator_and_empty_body_parses():
    # Header, a genuine blank-line separator, then truly nothing further:
    # this is a legitimate draft with an empty (but present) body — the
    # boundary case the missing-separator checks above must not swallow.
    text = "title: bear\nslug: 230919-bear\n\n"
    d = parse_draft(text)
    assert d.body == ""
    assert serialize_draft(d) == text


def test_unknown_key_is_a_warning_not_an_error():
    text = (
        "title: bear\n"
        "slug: 230919-bear\n"
        "mood: hungry\n"
        "\n"
        "synthetic body for testing\n"
    )
    d = parse_draft(text)
    assert d.title == "bear"
    assert d.slug == "230919-bear"
    assert len(d.warnings) == 1
    assert "mood" in d.warnings[0]


def test_unknown_key_is_carried_in_extra():
    text = "title: bear\nslug: 230919-bear\nmood: hungry\n\nsynthetic body for testing\n"
    d = parse_draft(text)
    assert d.extra == {"mood": "hungry"}


def test_unknown_keys_survive_a_round_trip_in_file_order():
    text = (
        "title: bear\n"
        "slug: 230919-bear\n"
        "mood: hungry\n"
        "weather: rain\n"
        "\n"
        "synthetic body for testing\n"
    )
    assert serialize_draft(parse_draft(text)) == text


def test_a_draft_with_no_unknown_keys_has_an_empty_extra():
    assert parse_draft(CANONICAL).extra == {}


def test_serialize_places_unknown_keys_after_the_known_ones():
    text = "title: bear\nmood: hungry\nslug: 230919-bear\n\nsynthetic body for testing\n"
    assert serialize_draft(parse_draft(text)) == (
        "title: bear\nslug: 230919-bear\nmood: hungry\n\nsynthetic body for testing\n"
    )


def test_serialize_parse_round_trip_for_canonical_file():
    assert serialize_draft(parse_draft(CANONICAL)) == CANONICAL


def test_serialize_omits_date_when_absent():
    text = "title: bear\nslug: 230919-bear\n\nsynthetic body for testing\n"
    assert serialize_draft(parse_draft(text)) == text


def test_drafts_dir_env_override(tmp_path, monkeypatch):
    override = tmp_path / "drafts-here"
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(override))
    result = drafts_dir()
    assert result == override
    assert override.is_dir()


def test_drafts_dir_default_is_not_touched_when_env_set(tmp_path, monkeypatch):
    # Regression guard: setting LOG_DRAFTS_DIR must fully redirect drafts_dir()
    # away from the real ~/Documents/log-drafts default.
    override = tmp_path / "isolated"
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(override))
    assert drafts_dir() == override


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    d = Draft(
        title="bear",
        slug="230919-bear",
        date="2023-09-19 08:00",
        body="synthetic body for testing\n",
        path=None,
    )
    saved = save_draft(d)
    assert saved.path == tmp_path / "230919-bear.md"
    assert saved.path.is_file()

    loaded = load_draft(saved.path)
    assert loaded.title == d.title
    assert loaded.slug == d.slug
    assert loaded.date == d.date
    assert loaded.body == d.body
    assert loaded.path == saved.path


def test_save_draft_respects_existing_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    explicit_path = tmp_path / "elsewhere.md"
    d = Draft(
        title="bear",
        slug="230919-bear",
        date=None,
        body="synthetic body for testing\n",
        path=explicit_path,
    )
    save_draft(d)
    assert explicit_path.is_file()
    assert not (tmp_path / "230919-bear.md").exists()


def test_list_drafts_returns_only_md_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    d1 = Draft(title="a", slug="a", date=None, body="synthetic body for testing\n", path=None)
    d2 = Draft(title="b", slug="b", date=None, body="synthetic body for testing\n", path=None)
    save_draft(d1)
    save_draft(d2)
    (tmp_path / "a.assets").mkdir()
    (tmp_path / "a.assets" / "pig.jpg").write_bytes(b"synthetic")

    found = list_drafts()
    assert sorted(p.name for p in found) == ["a.md", "b.md"]


def test_assets_dir_is_beside_the_draft_file(tmp_path):
    path = tmp_path / "230919-bear.md"
    d = Draft(
        title="bear",
        slug="230919-bear",
        date=None,
        body="synthetic body for testing\n",
        path=path,
    )
    assert assets_dir(d) == tmp_path / "230919-bear.assets"


def test_assets_dir_falls_back_to_drafts_dir_when_unsaved(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    d = Draft(
        title="bear",
        slug="230919-bear",
        date=None,
        body="synthetic body for testing\n",
        path=None,
    )
    assert assets_dir(d) == tmp_path / "230919-bear.assets"


def test_list_drafts_sorts_newest_name_first(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DRAFTS_DIR", str(tmp_path))
    for slug in ("220101-old", "231103-fox", "230919-bear"):
        save_draft(Draft(title=slug, slug=slug, date=None,
                         body="synthetic body for testing\n", path=None))
    found = [p.stem for p in list_drafts()]
    assert found == ["231103-fox", "230919-bear", "220101-old"]
