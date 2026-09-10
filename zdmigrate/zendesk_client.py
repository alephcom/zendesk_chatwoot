"""Zendesk REST client: auth, throttle/retry, cursor pagination, downloads."""
from __future__ import annotations

import time
from typing import Iterator, Optional
from urllib.parse import urlparse

import requests

from .logger import Logger


class ZendeskClient:
    def __init__(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        log: Logger,
        requests_per_minute: int = 600,
        max_retries: int = 6,
    ) -> None:
        self.base = f"https://{subdomain}.zendesk.com/api/v2/"
        self.log = log
        self.max_retries = max_retries
        self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._last = 0.0
        self.session = requests.Session()
        self.session.auth = (f"{email}/token", api_token)

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = self.base + url.lstrip("/")
        attempt = 0
        timeout = kwargs.pop("timeout", 120)
        while True:
            attempt += 1
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise
                wait = min(60, 2 ** attempt)
                self.log.warn(f"Network error on {url}: {exc}; retry in {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.max_retries:
                    resp.raise_for_status()
                wait = int(resp.headers.get("Retry-After", 0)) or min(60, 2 ** attempt)
                self.log.warn(f"HTTP {resp.status_code} on {url}; retry in {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} on {url}: {resp.text[:500]}")
            return resp

    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", url, params=params).json()

    def paginate(self, path: str, key: str, params: Optional[dict] = None) -> Iterator[dict]:
        """Cursor- or offset-paginate a list endpoint, yielding items under `key`."""
        params = dict(params or {})
        if "page[size]" not in params and "per_page" not in params:
            params["page[size]"] = 100
        url: Optional[str] = path
        first = True
        while url:
            data = self.get_json(url, params=params if first else None)
            first = False
            for item in data.get(key) or []:
                yield item
            url = data.get("next_page") or (data.get("links") or {}).get("next")
            params = None

    def incremental_tickets(self, start_url: Optional[str], start_time: int) -> Iterator[tuple[list[dict], Optional[str], bool]]:
        """Yield (tickets, after_url, end_of_stream) pages for checkpointing."""
        if start_url:
            url: Optional[str] = start_url
        else:
            url = f"incremental/tickets/cursor.json?start_time={int(start_time)}"
        while url:
            data = self.get_json(url)
            tickets = list(data.get("tickets") or [])
            after = data.get("after_url")
            eos = bool(data.get("end_of_stream"))
            yield tickets, after, eos
            if eos or not after:
                break
            url = after

    def ticket_comments(self, ticket_id: int) -> list[dict]:
        comments = list(self.paginate(f"tickets/{ticket_id}/comments.json", "comments"))
        return comments

    def download(self, content_url: str) -> bytes:
        """Try the token URL unauthenticated, then authenticated."""
        try:
            raw = requests.get(content_url, timeout=120)
            if raw.status_code == 200 and raw.content:
                return raw.content
        except requests.RequestException:
            pass
        resp = self._request("GET", content_url)
        return resp.content

    def download_filename(self, content_url: str, fallback: str) -> str:
        path = urlparse(content_url).path
        name = path.rsplit("/", 1)[-1] if path else ""
        return name or fallback
