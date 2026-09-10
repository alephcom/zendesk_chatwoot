"""Zendesk + storage config loaded from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ (does not override)."""
    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def require_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise SystemExit(f"Required env var {key} is not set (see .env.example).")
    return v


@dataclass
class Config:
    subdomain: str
    email: str
    api_token: str
    storage_dir: Path
    requests_per_minute: int
    export_start_time: int

    @property
    def dirs(self) -> dict[str, Path]:
        root = self.storage_dir
        return {
            "inventory": root / "inventory",
            "tickets": root / "tickets",
            "conversations": root / "conversations",
            "attachments": root / "attachments",
            "state": root / "state",
            "logs": root / "logs",
        }

    def ensure_dirs(self) -> None:
        for p in self.dirs.values():
            p.mkdir(parents=True, exist_ok=True)

    def require_zendesk(self) -> None:
        if not self.subdomain or not self.email or not self.api_token:
            raise SystemExit(
                "ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, and ZENDESK_API_TOKEN must be set."
            )


def load_config(*, require_zendesk: bool = False) -> Config:
    load_env()
    storage = Path(os.environ.get("STORAGE_DIR", "./storage")).expanduser()
    if not storage.is_absolute():
        storage = storage.resolve()
    rpm = int(os.environ.get("REQUESTS_PER_MINUTE", "600") or "600")
    start = int(os.environ.get("EXPORT_START_TIME", "0") or "0")
    cfg = Config(
        subdomain=os.environ.get("ZENDESK_SUBDOMAIN", "").strip(),
        email=os.environ.get("ZENDESK_EMAIL", "").strip(),
        api_token=os.environ.get("ZENDESK_API_TOKEN", "").strip(),
        storage_dir=storage,
        requests_per_minute=rpm,
        export_start_time=start,
    )
    cfg.ensure_dirs()
    if require_zendesk:
        cfg.require_zendesk()
    return cfg
