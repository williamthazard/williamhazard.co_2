import json

import httpx
import pytest

from writer.client import ClientError, WriterClient


def _client(handler, token="synthetic-client-token"):
    """A WriterClient wired to a MockTransport running `handler` — no network."""
    transport = httpx.MockTransport(handler)
    return WriterClient("https://example.test", token=token, transport=transport)


# --- ping -------------------------------------------------------------

def test_ping_happy_path():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/writer/ping"
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).ping() == {"ok": True}


def test_ping_401_raises_client_error():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ClientError) as excinfo:
        _client(handler).ping()
    assert str(excinfo.value) == "401 unauthorized"


def test_ping_sends_bearer_token_when_set():
    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-client-token"
        return httpx.Response(200, json={"ok": True})

    _client(handler, token="synthetic-client-token").ping()


def test_ping_omits_authorization_header_when_token_is_none():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"ok": True})

    _client(handler, token=None).ping()


def test_connection_error_raises_cannot_reach_host():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ClientError) as excinfo:
        _client(handler).ping()
    assert str(excinfo.value) == "cannot reach example.test"


# --- list_entries -------------------------------------------------------

def test_list_entries_happy_path_returns_the_list():
    def handler(request):
        assert request.url.path == "/api/writer/entries"
        return httpx.Response(200, json={"entries": [
            {"slug": "bear", "title": "Bear", "publish_date": "2023-09-19"},
        ]})

    entries = _client(handler).list_entries()
    assert entries == [{"slug": "bear", "title": "Bear", "publish_date": "2023-09-19"}]


def test_list_entries_error_mapping():
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(ClientError) as excinfo:
        _client(handler).list_entries()
    assert str(excinfo.value) == "500 Internal Server Error"


def test_list_entries_sends_bearer_token():
    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-client-token"
        return httpx.Response(200, json={"entries": []})

    _client(handler).list_entries()


# --- get_entry ------------------------------------------------------------

def test_get_entry_happy_path():
    def handler(request):
        assert request.url.path == "/api/writer/entries/bear"
        return httpx.Response(200, json={
            "slug": "bear", "title": "Bear", "content_markdown": "hi",
            "publish_date": "2023-09-19T08:00:00",
            "share_to_bluesky": False, "share_to_mastodon": False,
            "posted_to_bluesky": False, "posted_to_mastodon": False,
        })

    entry = _client(handler).get_entry("bear")
    assert entry["slug"] == "bear"
    assert entry["content_markdown"] == "hi"


def test_get_entry_404_raises_client_error():
    def handler(request):
        return httpx.Response(404, text="Not Found")

    with pytest.raises(ClientError) as excinfo:
        _client(handler).get_entry("missing")
    assert str(excinfo.value) == "404 Not Found"


def test_get_entry_omits_authorization_header_when_token_is_none():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"slug": "bear"})

    _client(handler, token=None).get_entry("bear")


# --- upsert_entry -----------------------------------------------------

def test_upsert_entry_happy_path_sends_put_with_json_body():
    def handler(request):
        assert request.method == "PUT"
        assert request.url.path == "/api/writer/entries/bear"
        body = json.loads(request.content)
        assert body["title"] == "Bear"
        assert body["content_markdown"] == "hello"
        assert body["share_to_bluesky"] is True
        assert body["share_to_mastodon"] is False
        assert "publish_date" not in body
        return httpx.Response(201, json={"status": "created", "slug": "bear"})

    result = _client(handler).upsert_entry("bear", "Bear", "hello", share_bluesky=True)
    assert result == {"status": "created", "slug": "bear"}


def test_upsert_entry_includes_publish_date_when_given():
    def handler(request):
        body = json.loads(request.content)
        assert body["publish_date"] == "2023-09-19T08:00:00"
        return httpx.Response(200, json={"status": "updated", "slug": "bear"})

    _client(handler).upsert_entry(
        "bear", "Bear", "hello", publish_date="2023-09-19T08:00:00",
    )


def test_upsert_entry_400_raises_client_error_with_server_text():
    def handler(request):
        return httpx.Response(400, json={"error": "title and content_markdown are required"})

    with pytest.raises(ClientError) as excinfo:
        _client(handler).upsert_entry("bear", "", "")
    assert str(excinfo.value) == "400 title and content_markdown are required"


def test_upsert_entry_sends_bearer_token():
    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-client-token"
        return httpx.Response(200, json={"status": "updated", "slug": "bear"})

    _client(handler).upsert_entry("bear", "Bear", "hello")


# --- upload_asset -----------------------------------------------------

def test_upload_asset_happy_path_sends_multipart():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/writer/assets"
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = request.content
        assert b'name="slug"' in body
        assert b"bear" in body
        assert b'name="name"' in body
        assert b'filename="pig.jpg"' in body
        assert b"synthetic-bytes" in body
        return httpx.Response(201, json={"status": "uploaded"})

    result = _client(handler).upload_asset("bear", "pig.jpg", b"synthetic-bytes")
    assert result == {"status": "uploaded"}


def test_upload_asset_unchanged_is_not_an_error():
    def handler(request):
        return httpx.Response(200, json={"status": "unchanged"})

    result = _client(handler).upload_asset("bear", "pig.jpg", b"synthetic-bytes")
    assert result == {"status": "unchanged"}


def test_upload_asset_409_raises_client_error_naming_the_asset():
    def handler(request):
        return httpx.Response(409, json={
            "status": "conflict",
            "error": "pig.jpg exists with different content",
        })

    with pytest.raises(ClientError) as excinfo:
        _client(handler).upload_asset("bear", "pig.jpg", b"different-bytes")
    assert str(excinfo.value) == "409 pig.jpg exists with different content"


def test_upload_asset_404_unknown_entry():
    def handler(request):
        return httpx.Response(404, json={"error": "unknown entry"})

    with pytest.raises(ClientError) as excinfo:
        _client(handler).upload_asset("nope", "pig.jpg", b"data")
    assert str(excinfo.value) == "404 unknown entry"


def test_upload_asset_sends_bearer_token():
    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-client-token"
        return httpx.Response(201, json={"status": "uploaded"})

    _client(handler).upload_asset("bear", "pig.jpg", b"synthetic-bytes")


def test_upload_asset_omits_authorization_header_when_token_is_none():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(201, json={"status": "uploaded"})

    _client(handler, token=None).upload_asset("bear", "pig.jpg", b"synthetic-bytes")


# --- token hygiene ------------------------------------------------------

def test_repr_excludes_token():
    c = _client(lambda r: httpx.Response(200), token="super-secret-synthetic-token")
    assert "super-secret-synthetic-token" not in repr(c)
    assert "WriterClient" in repr(c)
    assert "example.test" in repr(c)


def test_repr_excludes_token_field_entirely_when_none():
    c = _client(lambda r: httpx.Response(200), token=None)
    assert "token" not in repr(c).lower()
