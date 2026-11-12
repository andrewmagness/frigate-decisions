"""Fetch media from the Frigate HTTP API (or an arbitrary URL)."""

from __future__ import annotations

import logging
import time

import httpx

from .decisions import Media

log = logging.getLogger(__name__)

_KIND_BY_PREFIX = {"image/": "image", "video/": "video", "audio/": "audio"}


def _kind(mime: str) -> str:
    if mime == "application/pdf":
        return "document"
    for prefix, kind in _KIND_BY_PREFIX.items():
        if mime.startswith(prefix):
            return kind
    raise ValueError(f"unsupported media type {mime!r}")


class FrigateClient:
    def __init__(self, base_url: str, token: str | None = None, verify_ssl: bool = True, timeout: float = 30.0,
                 http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._frigate = http or httpx.Client(timeout=timeout, verify=verify_ssl, headers=headers)
        # Separate client for arbitrary URLs so the Frigate token never leaks to other hosts.
        self._plain = http or httpx.Client(timeout=timeout, verify=verify_ssl)

    def _get(self, client: httpx.Client, url: str, default_mime: str, attempts: int = 1, wait: float = 2.0) -> Media:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = client.get(url)
                if resp.status_code == 200 and resp.content:
                    mime = resp.headers.get("content-type", default_mime).split(";")[0].strip() or default_mime
                    if mime == "application/octet-stream":
                        mime = default_mime
                    return Media(data=resp.content, mime_type=mime, kind=_kind(mime))
                last = RuntimeError(f"GET {url} -> HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                last = exc
            if attempt + 1 < attempts:
                time.sleep(wait)
        raise RuntimeError(f"could not fetch media: {last}")

    def event_snapshot(self, event_id: str) -> Media:
        # The snapshot may lag a moment behind a brand-new review; retry briefly.
        return self._get(self._frigate, f"{self.base_url}/api/events/{event_id}/snapshot.jpg?bbox=0",
                         "image/jpeg", attempts=4, wait=1.5)

    def event_clip(self, event_id: str) -> Media:
        return self._get(self._frigate, f"{self.base_url}/api/events/{event_id}/clip.mp4",
                         "video/mp4", attempts=4, wait=3.0)

    def latest(self, camera: str, height: int = 1080) -> Media:
        return self._get(self._frigate, f"{self.base_url}/api/{camera}/latest.jpg?h={height}", "image/jpeg",
                         attempts=2)

    def url(self, url: str) -> Media:
        return self._get(self._plain, url, "image/jpeg", attempts=2)
