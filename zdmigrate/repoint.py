"""Move imported conversations from the API inbox onto the Email inbox.

Chatwoot's API cannot change conversation.inbox_id. This module writes SQL
(like fixup.py) that you apply with psql after the import.

Requires CHATWOOT_EMAIL_INBOX_ID in .env (the live Email channel). Replies
in that inbox send real customer email — do this after the import/fixup, and
keep Sidekiq paused if you are not ready for mail.

Run:
  python -m zdmigrate.repoint
  psql "$CHATWOOT_DATABASE_URL" -1 -f storage/state/repoint_inbox.sql
"""
from __future__ import annotations

import sys
from pathlib import Path

from .chatwoot_config import load_chatwoot_config
from .config import load_config
from .importer import load_import_progress


_INSERT_CHUNK = 500


def imported_display_ids(progress: dict[int, dict]) -> list[int]:
    """Chatwoot API conversation ids (display_id) for completed imports."""
    ids: set[int] = set()
    for rec in progress.values():
        if rec.get("status") != "complete":
            continue
        cid = rec.get("conversation_id")
        if cid:
            ids.add(int(cid))
    return sorted(ids)


def _values_rows(ids: list[int]) -> list[str]:
    chunks: list[str] = []
    for i in range(0, len(ids), _INSERT_CHUNK):
        part = ids[i : i + _INSERT_CHUNK]
        chunks.append(",\n".join(f"  ({n})" for n in part))
    return chunks


def generate_sql(
    display_ids: list[int],
    account_id: int,
    api_inbox_id: int,
    email_inbox_id: int,
) -> str:
    if api_inbox_id == email_inbox_id:
        raise ValueError("api_inbox_id and email_inbox_id must differ")
    acct = int(account_id)
    api = int(api_inbox_id)
    email = int(email_inbox_id)
    ids = [int(x) for x in display_ids]
    lines = [
        "-- Move imported conversations from the API inbox to the Email inbox.",
        "-- Chatwoot API cannot change inbox_id; apply this with psql.",
        "-- Review before running. Take a database backup first.",
        "-- Conversations without a contact email are left on the API inbox.",
        "-- Replies on the Email inbox will send real customer email.",
        "BEGIN;",
        "",
        "CREATE TEMP TABLE zd_repoint_display_ids (display_id integer PRIMARY KEY);",
        "",
    ]
    if ids:
        for chunk in _values_rows(ids):
            lines.append("INSERT INTO zd_repoint_display_ids (display_id) VALUES")
            lines.append(chunk + ";")
            lines.append("")
    lines.extend(
        [
            "-- ContactInbox rows for the Email channel (source_id = contact email).",
            "INSERT INTO contact_inboxes ("
            "contact_id, inbox_id, source_id, hmac_verified, pubsub_token, "
            "created_at, updated_at)",
            "SELECT DISTINCT c.contact_id,",
            f"  {email},",
            "  lower(trim(ct.email)),",
            "  false,",
            "  md5(random()::text || c.contact_id::text || clock_timestamp()::text),",
            "  NOW(), NOW()",
            "FROM conversations c",
            "JOIN contacts ct ON ct.id = c.contact_id",
            "JOIN zd_repoint_display_ids z ON z.display_id = c.display_id",
            f"WHERE c.account_id = {acct}",
            f"  AND c.inbox_id = {api}",
            "  AND coalesce(trim(ct.email), '') <> ''",
            "  AND NOT EXISTS (",
            "    SELECT 1 FROM contact_inboxes e",
            "    WHERE e.contact_id = c.contact_id AND e.inbox_id = "
            f"{email}",
            "  )",
            "ON CONFLICT (inbox_id, source_id) DO NOTHING;",
            "",
            "UPDATE conversations c",
            f"SET inbox_id = {email},",
            "    contact_inbox_id = e.id,",
            "    updated_at = NOW()",
            "FROM contact_inboxes e, contacts ct, zd_repoint_display_ids z",
            "WHERE c.account_id = "
            f"{acct}",
            f"  AND c.inbox_id = {api}",
            "  AND c.display_id = z.display_id",
            "  AND c.contact_id = e.contact_id",
            f"  AND e.inbox_id = {email}",
            "  AND ct.id = c.contact_id",
            "  AND coalesce(trim(ct.email), '') <> '';",
            "",
            "UPDATE messages m",
            f"SET inbox_id = {email},",
            "    updated_at = NOW()",
            "WHERE m.inbox_id = "
            f"{api}",
            "  AND m.conversation_id IN (",
            "    SELECT c.id FROM conversations c",
            "    JOIN zd_repoint_display_ids z ON z.display_id = c.display_id",
            f"    WHERE c.account_id = {acct}",
            f"      AND c.inbox_id = {email}",
            "  );",
            "",
            "COMMIT;",
            "",
        ]
    )
    return "\n".join(lines)


def generate() -> Path:
    cfg = load_config()
    cw = load_chatwoot_config()
    if not cw.email_inbox_id:
        sys.exit("Set CHATWOOT_EMAIL_INBOX_ID in .env (see .env.example).")
    if cw.email_inbox_id == cw.inbox_id:
        sys.exit("CHATWOOT_EMAIL_INBOX_ID must be different from CHATWOOT_INBOX_ID.")

    progress_path = cfg.dirs["state"] / "imported.jsonl"
    if not progress_path.is_file():
        sys.exit("No imported.jsonl — run the importer first.")
    ids = imported_display_ids(load_import_progress(progress_path))
    if not ids:
        sys.exit("No completed imports in imported.jsonl.")

    sql = generate_sql(ids, cw.account_id, cw.inbox_id, cw.email_inbox_id)
    out = cfg.dirs["state"] / "repoint_inbox.sql"
    out.write_text(sql, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    del argv
    path = generate()
    print(f"Wrote {path}")
    print("Apply with: psql \"$CHATWOOT_DATABASE_URL\" -1 -f " + str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
