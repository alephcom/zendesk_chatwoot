"""Chatwoot application API client (self-hosted).

Uses an administrator access token against /api/v1/accounts/{account_id}.
Sends both `api_access_token` (Chatwoot docs) and `api-access-token` so
proxies that drop underscore headers still pass the token through.
Depends only on `requests`.
"""
from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .logger import Logger


class ChatwootClient:
    def __init__(
        self,
        base_url: str,
        account_id: int,
        api_token: str,
        log: Logger,
        requests_per_minute: int = 600,
        max_retries: int = 6,
    ) -> None:
        self.acct_url = f"{base_url.rstrip('/')}/api/v1/accounts/{account_id}/"
        self.log = log
        self.max_retries = max_retries
        self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "api_access_token": api_token,
                "api-access-token": api_token,
            }
        )

    # -- contacts ----------------------------------------------------------

    def search_contact(self, query: str) -> list[dict]:
        data = self._json("GET", "contacts/search", params={"q": query})
        return data.get("payload", []) if isinstance(data, dict) else []

    def create_contact(
        self,
        name: str,
        email: Optional[str] = None,
        identifier: Optional[str] = None,
        custom_attributes: Optional[dict] = None,
    ) -> dict:
        body: dict = {"name": name or "Unknown"}
        if email:
            body["email"] = email
        if identifier:
            body["identifier"] = identifier
        if custom_attributes:
            body["custom_attributes"] = custom_attributes
        data = self._json("POST", "contacts", json=body)
        # create returns {payload: {contact: {...}, contact_inbox: {...}}}
        payload = data.get("payload", data)
        return payload.get("contact", payload)

    def create_contact_inbox(self, contact_id: int, inbox_id: int, source_id: Optional[str] = None) -> dict:
        body: dict = {"inbox_id": inbox_id}
        if source_id:
            body["source_id"] = source_id
        return self._json("POST", f"contacts/{contact_id}/contact_inboxes", json=body)

    # -- conversations & messages -----------------------------------------

    def create_conversation(self, payload: dict) -> dict:
        return self._json("POST", "conversations", json=payload)

    def create_message(
        self,
        conversation_id: int,
        content: str,
        message_type: str,
        private: bool,
        files: Optional[list[Path]] = None,
    ) -> dict:
        path = f"conversations/{conversation_id}/messages"
        if not files:
            body = {
                "content": content,
                "message_type": message_type,
                "private": private,
                "content_type": "text",
            }
            return self._json("POST", path, json=body)

        # multipart when there are attachments; booleans/enums go as form fields
        data = {
            "content": content,
            "message_type": message_type,
            "private": "true" if private else "false",
        }
        file_handles = []
        multipart = []
        try:
            for fp in files:
                fh = open(fp, "rb")
                file_handles.append(fh)
                mime = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
                multipart.append(("attachments[]", (fp.name, fh, mime)))
            return self._json("POST", path, data=data, files=multipart)
        finally:
            for fh in file_handles:
                fh.close()

    def add_labels(self, conversation_id: int, labels: list[str]) -> dict:
        return self._json("POST", f"conversations/{conversation_id}/labels", json={"labels": labels})

    def find_conversation_by_zendesk_id(self, zendesk_ticket_id: int) -> Optional[dict]:
        """Best-effort lookup; Chatwoot may not filter additional_attributes."""
        payload = {
            "payload": [
                {
                    "attribute_key": "additional_attributes",
                    "filter_operator": "equal_to",
                    "values": [f"zendesk_ticket_id:{zendesk_ticket_id}"],
                    "query_operator": None,
                }
            ]
        }
        try:
            data = self._json("POST", "conversations/filter", json=payload)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        items = data.get("payload") or data.get("data") or []
        if isinstance(items, dict):
            items = items.get("payload") or []
        for conv in items:
            attrs = conv.get("additional_attributes") or {}
            if str(attrs.get("zendesk_ticket_id")) == str(zendesk_ticket_id):
                return conv
        return None

    def list_agents(self) -> list[dict]:
        data = self._json("GET", "agents")
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict):
            items = data.get("payload") or data.get("data") or []
            if isinstance(items, list):
                return [a for a in items if isinstance(a, dict)]
        return []

    # -- internals ---------------------------------------------------------

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    def _json(self, method: str, path: str, **kwargs) -> Any:
        url = self.acct_url + path.lstrip("/")
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=120, **kwargs)
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
            return resp.json() if resp.content else {}
