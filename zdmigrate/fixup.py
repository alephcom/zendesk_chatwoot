"""Generate SQL that backdates Chatwoot records to original Zendesk timestamps.

Chatwoot's API exposes conversation `id` as display_id. Postgres `conversations.id`
is the internal PK — updates must use account_id + display_id.

last_activity_at is SET to the latest message timestamp (not GREATEST with "now").
"""
from __future__ import annotations

import json
from pathlib import Path

from .chatwoot_config import load_chatwoot_config
from .config import load_config


def _iso(ts: str) -> str:
    return ts.replace("'", "''")


def records_from_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def generate_sql(records: list[dict], account_id: int) -> str:
    """Build a single-transaction SQL script from timestamps.jsonl records."""
    msg_updates: list[str] = []
    conv_created: dict[int, str] = {}
    last_activity: dict[int, str] = {}

    for rec in records:
        ts = rec.get("created_at")
        if not ts:
            continue
        iso = _iso(str(ts))
        kind = rec.get("kind")
        if kind == "conversation":
            display_id = int(rec.get("display_id") or rec.get("id") or 0)
            if display_id:
                conv_created[display_id] = iso
                last_activity.setdefault(display_id, iso)
        elif kind == "message":
            mid = int(rec.get("id") or 0)
            if mid:
                msg_updates.append(
                    f"UPDATE messages SET created_at = '{iso}' WHERE id = {mid};"
                )
            display_id = int(rec.get("display_id") or 0)
            if display_id:
                prev = last_activity.get(display_id)
                if prev is None or iso > prev:
                    last_activity[display_id] = iso

    conv_updates: list[str] = []
    for display_id, created in conv_created.items():
        activity = last_activity.get(display_id, created)
        conv_updates.append(
            f"UPDATE conversations SET created_at = '{created}', "
            f"last_activity_at = '{activity}' "
            f"WHERE account_id = {int(account_id)} AND display_id = {display_id};"
        )

    lines = [
        "-- Backdate Chatwoot records to original Zendesk timestamps.",
        "-- Review before running. Take a database backup first.",
        "-- conversations.id is the PK; API id is display_id.",
        "BEGIN;",
        "",
        f"-- {len(msg_updates)} messages",
        *msg_updates,
        "",
        f"-- {len(conv_updates)} conversations",
        *conv_updates,
        "",
        "COMMIT;",
        "",
    ]
    return "\n".join(lines)


def generate() -> Path:
    cfg = load_config()
    cw = load_chatwoot_config()
    src = cfg.dirs["state"] / "timestamps.jsonl"
    if not src.is_file():
        raise SystemExit("No timestamps.jsonl — run the importer first.")
    records = records_from_jsonl(src.read_text(encoding="utf-8"))
    sql = generate_sql(records, cw.account_id)
    out = cfg.dirs["state"] / "timestamp_fixup.sql"
    out.write_text(sql, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = generate()
    print(f"Wrote {path}")
