"""Export Zendesk data to disk (resumable stages: inventory | tickets | content)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import load_config
from .logger import Logger
from .transform import inline_images_from_body
from .zendesk_client import ZendeskClient

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str, fallback: str = "file") -> str:
    cleaned = SAFE_NAME.sub("_", name).strip("._") or fallback
    return cleaned[:180]


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def export_inventory(client: ZendeskClient, dirs: dict[str, Path], log: Logger) -> None:
    inv = dirs["inventory"]
    collections = {
        "brands": "brands",
        "groups": "groups",
        "ticket_fields": "ticket_fields",
        "users": "users",
        "organizations": "organizations",
    }
    for filename, key in collections.items():
        log.info(f"Exporting {key}…")
        items = list(client.paginate(f"{filename}.json", key))
        _write_json(inv / f"{filename}.json", items)
        log.info(f"  {len(items)} {key}")

    users = json.loads((inv / "users.json").read_text(encoding="utf-8"))
    agent_ids = [
        int(u["id"])
        for u in users
        if str(u.get("role") or "").lower() in {"agent", "admin"}
    ]
    _write_json(inv / "agent_ids.json", agent_ids)
    log.info(f"  {len(agent_ids)} agent/admin ids")


def export_tickets(client: ZendeskClient, dirs: dict[str, Path], start_time: int, log: Logger) -> None:
    state = dirs["state"]
    out = dirs["tickets"] / "tickets.jsonl"
    cursor_path = state / "tickets.cursor"
    done_path = state / "tickets.complete"

    if done_path.is_file():
        log.info("Ticket list already complete; skipping. Delete state/tickets.complete to re-export.")
        return

    start_url = cursor_path.read_text(encoding="utf-8").strip() if cursor_path.is_file() else ""
    mode = "a" if start_url else "w"
    written = 0
    with open(out, mode, encoding="utf-8") as fh:
        for tickets, after_url, eos in client.incremental_tickets(start_url or None, start_time):
            for t in tickets:
                if str(t.get("status") or "").lower() == "deleted":
                    continue
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
                written += 1
            fh.flush()
            if after_url:
                cursor_path.write_text(after_url + "\n", encoding="utf-8")
            if eos:
                done_path.write_text("1\n", encoding="utf-8")
                log.info(f"Tickets complete (+{written} this run).")
                return
    log.info(f"Tickets page written (+{written} this run). Re-run to continue.")


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return "." + path.rsplit(".", 1)[-1]
    return ".bin"


def export_content(client: ZendeskClient, dirs: dict[str, Path], log: Logger) -> None:
    tickets_path = dirs["tickets"] / "tickets.jsonl"
    if not tickets_path.is_file():
        raise SystemExit("Missing tickets.jsonl — run `python -m zdmigrate.export tickets` first.")

    done_path = dirs["state"] / "content.done"
    done: set[int] = set()
    if done_path.is_file():
        for line in done_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                done.add(int(line))

    n = 0
    with open(done_path, "a", encoding="utf-8") as done_out:
        for raw in open(tickets_path, encoding="utf-8"):
            raw = raw.strip()
            if not raw:
                continue
            ticket = json.loads(raw)
            tid = int(ticket.get("id") or 0)
            if not tid or tid in done:
                continue
            try:
                comments = client.ticket_comments(tid)
            except Exception as exc:  # noqa: BLE001
                log.error(f"Ticket {tid} comments failed: {exc}")
                continue

            conv_path = dirs["conversations"] / f"{tid}.json"
            _write_json(conv_path, comments)

            att_dir = dirs["attachments"] / str(tid)
            for comment in comments:
                cid = comment.get("id")
                for att in comment.get("attachments") or []:
                    url = att.get("content_url") or att.get("mapped_content_url")
                    if not url:
                        continue
                    aid = att.get("id", "att")
                    fname = _safe(att.get("file_name") or att.get("filename") or "file")
                    dest = att_dir / f"{aid}__{fname}"
                    if dest.is_file() and dest.stat().st_size > 0:
                        continue
                    att_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        dest.write_bytes(client.download(url))
                    except Exception as exc:  # noqa: BLE001
                        log.warn(f"Ticket {tid} attachment {aid} failed: {exc}")

                body = comment.get("plain_body") or comment.get("body") or comment.get("text") or ""
                for i, url in enumerate(inline_images_from_body(body)):
                    ext = _ext_from_url(url)
                    dest = att_dir / f"inline_{cid}_{i}{ext}"
                    if dest.is_file() and dest.stat().st_size > 0:
                        continue
                    att_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        dest.write_bytes(client.download(url))
                    except Exception as exc:  # noqa: BLE001
                        log.warn(f"Ticket {tid} inline {cid}/{i} failed: {exc}")

            done_out.write(f"{tid}\n")
            done_out.flush()
            done.add(tid)
            n += 1
            if n % 50 == 0:
                log.info(f"Content exported for {n} tickets this run")
    log.info(f"Content stage done (+{n} tickets this run).")


def main(argv: list[str]) -> int:
    cfg = load_config(require_zendesk=True)
    log = Logger(cfg.dirs["logs"] / "export.log")
    client = ZendeskClient(
        cfg.subdomain, cfg.email, cfg.api_token, log, cfg.requests_per_minute
    )
    stages = [a for a in argv[1:] if not a.startswith("-")]
    if not stages:
        stages = ["inventory", "tickets", "content"]
    known = {"inventory", "tickets", "content"}
    for s in stages:
        if s not in known:
            raise SystemExit(f"Unknown stage {s!r}; use inventory|tickets|content")
    if "inventory" in stages:
        export_inventory(client, cfg.dirs, log)
    if "tickets" in stages:
        export_tickets(client, cfg.dirs, cfg.export_start_time, log)
    if "content" in stages:
        export_content(client, cfg.dirs, log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
