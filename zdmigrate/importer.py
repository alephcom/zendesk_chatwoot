"""Import exported Zendesk data into Chatwoot via the application API.

Prerequisites (see README):
  * Chatwoot reachable, an admin api_access_token, and a SINGLE **API-type**
    inbox created for the import. An Api inbox is required so that creating
    outgoing (agent) messages does NOT send real emails to customers.
  * `python -m zdmigrate.export` already run (storage/ populated).
  * Agents should already exist in Chatwoot (matched by email). Optional
    mapping/agent_map.json overrides ({zendesk_agent_id: chatwoot_agent_id}).
    Unmatched assignees are left unassigned (a warning is logged).

What it does per ticket (idempotent — safe to re-run):
  1. resolve/create the requester contact (dedupe by email) + contact_inbox
  2. create the conversation in the one inbox, tagged with zendesk_ticket_id
  3. create each cleaned message (skips boilerplate-only), uploading attachments
     then rehosted inline images on the same message
  4. apply tags as labels; private note if the Zendesk ticket was merged
  5. restore pending/resolved status (Chatwoot reopens on incoming messages)
  6. record Chatwoot display ids + original Zendesk timestamps for SQL backdating

Run:
  python -m zdmigrate.importer --dry-run   # counts only; no Chatwoot writes
  python -m zdmigrate.importer --limit 50
  python -m zdmigrate.importer
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, Optional

from .chatwoot_config import load_chatwoot_config
from .config import load_config
from .logger import Logger
from .transform import (
    api_message_content,
    clean_comment,
    find_merge_target,
    map_custom_fields,
    map_create_status,
    map_status,
)


# --- pure helpers (unit-tested) --------------------------------------------

def build_conversation_payload(
    ticket: dict,
    contact_id: int,
    source_id: str,
    inbox_id: int,
    field_map: dict[int, str] | None = None,
) -> dict:
    add_attrs: dict = {"zendesk_ticket_id": ticket.get("id")}
    subject = (ticket.get("subject") or "").strip()
    if subject:
        add_attrs["mail_subject"] = subject
    payload: dict = {
        "source_id": source_id,
        "inbox_id": inbox_id,
        "contact_id": contact_id,
        "status": ticket_create_status(ticket),
        "additional_attributes": add_attrs,
    }
    custom = map_custom_fields(ticket.get("custom_fields", []), field_map)
    if custom:
        payload["custom_attributes"] = custom
    return payload


def ticket_create_status(ticket: dict) -> str:
    """Chatwoot create/toggle status for this Zendesk ticket (open/pending/resolved)."""
    return map_create_status(map_status(ticket.get("status", "")))


def load_import_progress(path: Path) -> dict[int, dict]:
    """Last-line-wins map of zendesk_ticket_id -> progress record."""
    done: dict[int, dict] = {}
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        tid = rec.get("zendesk_ticket_id")
        if tid is None:
            continue
        done[int(tid)] = rec
    return done


def load_agent_map(path: Path) -> dict[int, int]:
    """Ignore non-integer keys such as _comment in the example file."""
    if not path.is_file():
        return {}
    out: dict[int, int] = {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def load_custom_field_map(path: Path) -> dict[int, str]:
    """Zendesk field id -> Chatwoot attribute name. Ignore _comment keys."""
    if not path.is_file():
        return {}
    out: dict[int, str] = {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def chatwoot_agents_by_email(agents: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in agents or []:
        email = (a.get("email") or "").strip().lower()
        if email and a.get("id") is not None:
            out[email] = int(a["id"])
    return out


def build_assignee_map(
    users: dict[int, dict],
    zendesk_agent_ids: Iterable[int],
    cw_by_email: dict[str, int],
    override: dict[int, int],
) -> tuple[dict[int, int], list[str]]:
    """Map Zendesk agent ids to Chatwoot agent ids by email.

    `override` (agent_map.json) wins. Unmatched agents are omitted and listed
    in warnings — they are not created.
    """
    out = dict(override)
    warnings: list[str] = []
    for zd_id in zendesk_agent_ids:
        zd_id = int(zd_id)
        if zd_id in out:
            continue
        email = (users.get(zd_id, {}).get("email") or "").strip().lower()
        if not email:
            warnings.append(f"Zendesk agent {zd_id} has no email; cannot match a Chatwoot agent")
            continue
        cw_id = cw_by_email.get(email)
        if cw_id:
            out[zd_id] = cw_id
        else:
            warnings.append(
                f"No Chatwoot agent matching {email} (Zendesk user {zd_id}); "
                "tickets assigned to them will be left unassigned"
            )
    return out, warnings


def resolve_assignee_id(
    zendesk_assignee_id: int,
    users: dict[int, dict],
    assignee_map: dict[int, int],
    cw_by_email: dict[str, int],
) -> Optional[int]:
    if not zendesk_assignee_id:
        return None
    if zendesk_assignee_id in assignee_map:
        return assignee_map[zendesk_assignee_id]
    email = (users.get(zendesk_assignee_id, {}).get("email") or "").strip().lower()
    if email and email in cw_by_email:
        return cw_by_email[email]
    return None


def files_for_comment(att_dir: Path, comment: dict) -> list[Path]:
    """Real attachments first, then inline images for the same comment."""
    if not att_dir.is_dir():
        return []
    files: list[Path] = []
    for att in comment.get("attachments") or []:
        aid = str(att.get("id", ""))
        if aid:
            files += sorted(att_dir.glob(f"{aid}__*"))
    cid = comment.get("id", "")
    files += sorted(att_dir.glob(f"inline_{cid}_*"))
    return files


def _write_progress(fh, rec: dict) -> None:
    fh.write(json.dumps(rec) + "\n")
    fh.flush()


# --- data loaders ----------------------------------------------------------

def _load_users(dirs) -> dict[int, dict]:
    path = dirs["inventory"] / "users.json"
    if not path.is_file():
        sys.exit("Missing inventory/users.json — run `export inventory` first.")
    return {int(u["id"]): u for u in json.loads(path.read_text(encoding="utf-8"))}


def _load_agent_ids(dirs) -> set[int]:
    path = dirs["inventory"] / "agent_ids.json"
    if not path.is_file():
        sys.exit("Missing inventory/agent_ids.json — run `export inventory` first.")
    ids = {int(x) for x in json.loads(path.read_text(encoding="utf-8"))}
    if not ids:
        sys.exit("inventory/agent_ids.json is empty — refusing to import (all messages would be incoming).")
    return ids


# --- contact / ticket import ----------------------------------------------

def _resolve_contact(client, users, requester_id, cache, inbox_id) -> tuple[int, str]:
    if requester_id in cache:
        return cache[requester_id]
    user = users.get(requester_id, {})
    name = user.get("name") or "Unknown"
    email = user.get("email") or None
    identifier = f"zd-user-{requester_id}"

    contact = None
    if email:
        for c in client.search_contact(email):
            if (c.get("email") or "").lower() == email.lower():
                contact = c
                break
    if contact is None:
        contact = client.create_contact(name=name, email=email, identifier=identifier)

    contact_id = int(contact["id"])
    source_id = email or identifier
    try:
        ci = client.create_contact_inbox(contact_id, inbox_id, source_id)
        source_id = ci.get("source_id", source_id)
    except Exception:  # noqa: BLE001 — contact_inbox may already exist
        pass

    cache[requester_id] = (contact_id, source_id)
    return contact_id, source_id


def _import_ticket(
    client,
    cfg,
    cw,
    ticket,
    users,
    agent_ids,
    agent_map,
    field_map,
    cw_by_email,
    unmatched_assignees: set,
    cache,
    ts_out,
    progress_out,
    progress: dict,
    log,
) -> dict:
    tid = int(ticket["id"])
    requester_id = int(ticket.get("requester_id") or 0)
    contact_id, source_id = _resolve_contact(client, users, requester_id, cache, cw.inbox_id)

    conv_path = cfg.dirs["conversations"] / f"{tid}.json"
    comments = json.loads(conv_path.read_text(encoding="utf-8")) if conv_path.is_file() else []
    att_dir = cfg.dirs["attachments"] / str(tid)

    last_comment_id = 0
    message_count = 0
    file_count = 0
    conv_id: Optional[int] = None

    existing = progress.get(tid)
    if existing and existing.get("conversation_id"):
        conv_id = int(existing["conversation_id"])
        last_comment_id = int(existing.get("last_comment_id") or 0)
        message_count = int(existing.get("message_count") or 0)
        file_count = int(existing.get("file_count") or 0)
    else:
        found = client.find_conversation_by_zendesk_id(tid)
        if found and found.get("id"):
            conv_id = int(found["id"])
            log.warn(f"Ticket {tid}: reused existing Chatwoot conversation {conv_id}")

    if conv_id is None:
        payload = build_conversation_payload(ticket, contact_id, source_id, cw.inbox_id, field_map)
        zd_assignee = int(ticket.get("assignee_id") or 0)
        assignee = resolve_assignee_id(zd_assignee, users, agent_map, cw_by_email)
        if assignee:
            payload["assignee_id"] = assignee
        elif zd_assignee and zd_assignee not in unmatched_assignees:
            unmatched_assignees.add(zd_assignee)
            email = (users.get(zd_assignee, {}).get("email") or "").strip() or "no-email"
            log.warn(
                f"No Chatwoot agent for Zendesk assignee {zd_assignee} ({email}); "
                f"ticket {tid} left unassigned"
            )
        conv = client.create_conversation(payload)
        conv_id = int(conv["id"])
        ts_out.write(json.dumps({
            "kind": "conversation",
            "display_id": conv_id,
            "created_at": ticket.get("created_at"),
        }) + "\n")
        rec = {
            "zendesk_ticket_id": tid,
            "conversation_id": conv_id,
            "last_comment_id": 0,
            "status": "partial",
            "message_count": 0,
            "file_count": 0,
        }
        _write_progress(progress_out, rec)
        progress[tid] = rec

    for comment in comments:
        cid = int(comment.get("id") or 0)
        if cid and cid <= last_comment_id:
            continue
        msg = clean_comment(comment, agent_ids)
        files = files_for_comment(att_dir, comment)
        if msg.is_empty and not files:
            if cid:
                last_comment_id = cid
            continue
        content = api_message_content(msg.content, bool(files))
        created = client.create_message(
            conversation_id=conv_id,
            content=content,
            message_type=msg.message_type,
            private=msg.private,
            files=files or None,
        )
        mid = created.get("id")
        if mid:
            ts_out.write(json.dumps({
                "kind": "message",
                "id": int(mid),
                "display_id": conv_id,
                "created_at": msg.created_at,
            }) + "\n")
        message_count += 1
        file_count += len(files)
        if cid:
            last_comment_id = cid
        rec = {
            "zendesk_ticket_id": tid,
            "conversation_id": conv_id,
            "last_comment_id": last_comment_id,
            "status": "partial",
            "message_count": message_count,
            "file_count": file_count,
        }
        _write_progress(progress_out, rec)
        progress[tid] = rec

    merge_id = find_merge_target(ticket, comments)
    labels = list(ticket.get("tags") or [])
    if merge_id:
        labels.append("zendesk-merged")
        try:
            client.create_message(
                conversation_id=conv_id,
                content=f"This Zendesk ticket was merged into request #{merge_id}.",
                message_type="outgoing",
                private=True,
            )
            message_count += 1
        except Exception as exc:  # noqa: BLE001
            log.warn(f"Ticket {tid}: merge note failed: {exc}")
    if labels:
        try:
            client.add_labels(conv_id, labels)
        except Exception as exc:  # noqa: BLE001
            log.warn(f"Ticket {tid}: label apply failed: {exc}")

    # Incoming messages reopen resolved/pending conversations; restore after
    # the thread is fully written.
    cw_status = ticket_create_status(ticket)
    if cw_status != "open":
        try:
            client.toggle_status(conv_id, cw_status)
        except Exception as exc:  # noqa: BLE001
            log.warn(f"Ticket {tid}: status restore to {cw_status} failed: {exc}")

    rec = {
        "zendesk_ticket_id": tid,
        "conversation_id": conv_id,
        "last_comment_id": last_comment_id,
        "status": "complete",
        "message_count": message_count,
        "file_count": file_count,
    }
    _write_progress(progress_out, rec)
    progress[tid] = rec
    ts_out.flush()
    return rec


def dry_run(limit: Optional[int] = None) -> int:
    cfg = load_config()
    log = Logger(cfg.dirs["logs"] / "import.log")
    users = _load_users(cfg.dirs)
    agent_ids = _load_agent_ids(cfg.dirs)
    progress = load_import_progress(cfg.dirs["state"] / "imported.jsonl")

    tickets_path = cfg.dirs["tickets"] / "tickets.jsonl"
    if not tickets_path.is_file():
        sys.exit("Missing tickets.jsonl — run the export first.")

    contacts = 0
    conversations = 0
    messages = 0
    no_email: list[int] = []
    seen_requesters: set[int] = set()
    scanned = 0

    with open(tickets_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ticket = json.loads(line)
            tid = int(ticket.get("id") or 0)
            if not tid:
                continue
            rec = progress.get(tid)
            if rec and rec.get("status") == "complete":
                continue
            if limit is not None and scanned >= limit:
                break
            scanned += 1

            rid = int(ticket.get("requester_id") or 0)
            user = users.get(rid, {})
            email = user.get("email")
            if not email:
                no_email.append(tid)
            if rid not in seen_requesters:
                seen_requesters.add(rid)
                contacts += 1
            conversations += 1

            comments = []
            conv_path = cfg.dirs["conversations"] / f"{tid}.json"
            if conv_path.is_file():
                comments = json.loads(conv_path.read_text(encoding="utf-8"))
            att_dir = cfg.dirs["attachments"] / str(tid)
            last_cid = int((rec or {}).get("last_comment_id") or 0)
            for comment in comments:
                cid = int(comment.get("id") or 0)
                if cid and cid <= last_cid:
                    continue
                msg = clean_comment(comment, agent_ids)
                files = files_for_comment(att_dir, comment)
                if msg.is_empty and not files:
                    continue
                messages += 1
            if find_merge_target(ticket, comments):
                messages += 1  # private merge note

    log.info(
        f"Dry-run: would create ~{contacts} contacts, {conversations} conversations, "
        f"{messages} messages. Tickets without requester email: {len(no_email)}"
        + (f" {no_email[:20]}{'…' if len(no_email) > 20 else ''}" if no_email else "")
    )
    print(
        f"contacts={contacts} conversations={conversations} messages={messages} "
        f"no_requester_email={len(no_email)}"
    )
    return 0


def run_import(limit: Optional[int] = None) -> int:
    cfg = load_config()
    cw = load_chatwoot_config()
    root = Path(__file__).resolve().parent.parent
    log = Logger(cfg.dirs["logs"] / "import.log")
    from .chatwoot_client import ChatwootClient
    client = ChatwootClient(cw.url, cw.account_id, cw.api_token, log, cfg.requests_per_minute)

    users = _load_users(cfg.dirs)
    agent_ids = _load_agent_ids(cfg.dirs)
    override = load_agent_map(root / "mapping" / "agent_map.json")
    field_map = load_custom_field_map(root / "mapping" / "custom_field_map.json")
    try:
        cw_agents = client.list_agents()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"Could not list Chatwoot agents ({exc}); assignees will rely on agent_map.json only")
        cw_agents = []
    cw_by_email = chatwoot_agents_by_email(cw_agents)
    agent_map, map_warnings = build_assignee_map(users, agent_ids, cw_by_email, override)
    for w in map_warnings:
        log.warn(w)
    log.info(f"Assignee map: {len(agent_map)} Zendesk agents matched to Chatwoot")
    unmatched_assignees: set[int] = set()

    done_file = cfg.dirs["state"] / "imported.jsonl"
    ts_file = cfg.dirs["state"] / "timestamps.jsonl"
    progress = load_import_progress(done_file)

    tickets_path = cfg.dirs["tickets"] / "tickets.jsonl"
    if not tickets_path.is_file():
        sys.exit("Missing tickets.jsonl — run the export first.")

    contact_cache: dict[int, tuple[int, str]] = {}
    imported = 0

    with open(done_file, "a", encoding="utf-8") as done_out, open(ts_file, "a", encoding="utf-8") as ts_out:
        with open(tickets_path, encoding="utf-8") as ticket_fh:
            for line in ticket_fh:
                line = line.strip()
                if not line:
                    continue
                ticket = json.loads(line)
                tid = int(ticket.get("id") or 0)
                if not tid:
                    continue
                rec = progress.get(tid)
                if rec and rec.get("status") == "complete":
                    continue
                if limit is not None and imported >= limit:
                    break

                try:
                    _import_ticket(
                        client, cfg, cw, ticket, users, agent_ids, agent_map, field_map,
                        cw_by_email, unmatched_assignees,
                        contact_cache, ts_out, done_out, progress, log,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error(f"Ticket {tid} failed: {exc}")
                    continue

                imported += 1
                if imported % 50 == 0:
                    log.info(f"Imported {imported} tickets this run")

    log.info(
        f"Import done: {imported} tickets this run. "
        f"Next: generate + apply the timestamp fixup (python -m zdmigrate.fixup)."
    )
    return 0


def main(argv: list[str]) -> int:
    limit = None
    dry = "--dry-run" in argv
    if "--limit" in argv:
        i = argv.index("--limit")
        limit = int(argv[i + 1])
    if dry:
        return dry_run(limit=limit)
    return run_import(limit=limit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
