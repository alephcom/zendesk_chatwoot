"""Chatwoot connection settings, loaded from .env."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .config import load_env


@dataclass(frozen=True)
class ChatwootConfig:
    url: str
    account_id: int
    api_token: str
    inbox_id: int


def load_chatwoot_config() -> ChatwootConfig:
    load_env()

    def req(key: str) -> str:
        v = os.environ.get(key, "").strip()
        if not v:
            sys.exit(f"Required env var {key} is not set (see .env.example).")
        return v

    return ChatwootConfig(
        url=req("CHATWOOT_URL"),
        account_id=int(req("CHATWOOT_ACCOUNT_ID")),
        api_token=req("CHATWOOT_API_TOKEN"),
        inbox_id=int(req("CHATWOOT_INBOX_ID")),
    )
