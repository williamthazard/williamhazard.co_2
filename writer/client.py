"""HTTP client for the writer API (`website/api.py`'s `/api/writer/` routes).

Plain `httpx`, no Django import, no shared code with the server — this
package, and its dependencies, must never be a server dependency (same
rule `writer/draft.py` states for the header grammar).

Every failure — a non-2xx response, the request never reaching the server
at all, or a 2xx whose body is not the JSON the route promises — surfaces
as a single `ClientError` carrying a one-line HTTP truth (`"401
unauthorized"`, `"409 pig.jpg exists with different content"`, `"cannot
reach example.test"`, `"unexpected response from example.test"`). Callers
never inspect status codes or response bodies themselves, and never have
to guard against a second kind of exception: a proxy's sign-in page
answered with a 200, or a route that drifts and stops sending the key it
promised, is a `ClientError` like any other failure.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx


class ClientError(Exception):
    """A request to the writer API failed. `str(e)` is a one-line HTTP truth."""


class WriterClient:
    """Talks to one writer API server.

    `token` is sent as `Authorization: Bearer <token>` on every request
    when set; when `token` is `None`, no Authorization header is sent at
    all (the local DEBUG server allows that). The token is never logged,
    printed, or included in `repr()`.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, transport=transport)

    def __repr__(self) -> str:
        return f"WriterClient(base_url={str(self._client.base_url)!r})"

    def close(self) -> None:
        self._client.close()

    def ping(self) -> dict:
        return self._request("GET", "/api/writer/ping")

    def list_entries(self) -> list[dict]:
        payload = self._request("GET", "/api/writer/entries")
        try:
            return payload["entries"]
        except (KeyError, TypeError):
            raise ClientError(self._unexpected()) from None

    def list_entries_full(self) -> list[dict]:
        payload = self._request("GET", "/api/writer/entries", params={"full": "1"})
        try:
            return payload["entries"]
        except (KeyError, TypeError):
            raise ClientError(self._unexpected()) from None

    def list_assets(self, slug: str) -> list[dict]:
        payload = self._request("GET", f"/api/writer/entries/{slug}/assets")
        try:
            return payload["assets"]
        except (KeyError, TypeError):
            raise ClientError(self._unexpected()) from None

    def download_asset(self, slug: str, name: str) -> bytes:
        """Fetch one asset's bytes. `name` is a filename, not a URL fragment.

        Percent-encoded with `safe=""` so that every character in it stays
        inside the one path segment it belongs to: a `?` or a `#` written
        straight into the URL would be read as the start of a query string
        or a fragment, and the server would be asked for a shorter name
        than the one its own asset list gave.
        """
        try:
            response = self._client.get(
                f"/api/writer/assets/{slug}/{quote(name, safe='')}"
            )
        except httpx.TransportError:
            raise ClientError(f"cannot reach {self._client.base_url.host}") from None
        if response.status_code >= 400:
            raise ClientError(_error_message(response))
        return response.content

    def get_entry(self, slug: str) -> dict:
        return self._request("GET", f"/api/writer/entries/{slug}")

    def upsert_entry(
        self,
        slug: str,
        title: str,
        content_markdown: str,
        publish_date: str | None = None,
        share_bluesky: bool = False,
        share_mastodon: bool = False,
    ) -> dict:
        payload = {
            "title": title,
            "content_markdown": content_markdown,
            "share_to_bluesky": share_bluesky,
            "share_to_mastodon": share_mastodon,
        }
        if publish_date is not None:
            payload["publish_date"] = publish_date
        return self._request("PUT", f"/api/writer/entries/{slug}", json=payload)

    def upload_asset(self, slug: str, name: str, data: bytes) -> dict:
        form = {"slug": slug, "name": name}
        files = {"file": (name, data)}
        return self._request("POST", "/api/writer/assets", data=form, files=files)

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TransportError:
            raise ClientError(f"cannot reach {self._client.base_url.host}") from None
        if response.status_code >= 400:
            raise ClientError(_error_message(response))
        try:
            return response.json()
        except ValueError:
            raise ClientError(self._unexpected()) from None

    def _unexpected(self) -> str:
        """The truth for a 2xx that isn't the JSON the route promised."""
        return f"unexpected response from {self._client.base_url.host}"


def _error_message(response: httpx.Response) -> str:
    detail = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        detail = body["error"]
    return f"{response.status_code} {detail if detail else response.reason_phrase}"
