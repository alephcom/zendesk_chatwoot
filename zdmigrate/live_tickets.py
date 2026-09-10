"""Read-only report of non-closed Zendesk tickets (cutover helpers).

Run: python -m zdmigrate.live_tickets
No writes to Zendesk.
"""
from __future__ import annotations

import json
import sys

from .config import load_config
from .logger import Logger

LIVE_STATUSES = {"new", "open", "pending", "hold", "solved"}


def live_from_export(tickets: list[dict]) -> list[dict]:
    out = []
    for t in tickets:
        st = str(t.get("status") or "").lower()
        if st in LIVE_STATUSES:
            out.append(t)
    return out


def format_row(ticket: dict, users: dict[int, dict]) -> str:
    tid = ticket.get("id")
    st = ticket.get("status")
    subj = (ticket.get("subject") or "(no subject)")[:80]
    rid = int(ticket.get("requester_id") or 0)
    aid = int(ticket.get("assignee_id") or 0)
    req = users.get(rid, {})
    asg = users.get(aid, {})
    req_s = req.get("email") or req.get("name") or rid or "-"
    asg_s = asg.get("name") or asg.get("email") or (aid or "unassigned")
    updated = ticket.get("updated_at") or ticket.get("created_at") or ""
    return f"#{tid}  {st:8}  {updated}  requester={req_s}  assignee={asg_s}  {subj}"


def tickets_from_zendesk(client) -> list[dict]:
    """Search each live status (read-only)."""
    found: dict[int, dict] = {}
    for status in sorted(LIVE_STATUSES):
        for t in client.paginate(
            "search.json",
            "results",
            params={"query": f"type:ticket status:{status}", "per_page": 100},
        ):
            if t.get("id"):
                found[int(t["id"])] = t
    return list(found.values())


def main(argv: list[str]) -> int:
    cfg = load_config(require_zendesk=True)
    log = Logger(cfg.dirs["logs"] / "live_tickets.log")
    from .zendesk_client import ZendeskClient
    client = ZendeskClient(
        cfg.subdomain, cfg.email, cfg.api_token, log, cfg.requests_per_minute,
    )
    users_path = cfg.dirs["inventory"] / "users.json"
    users: dict[int, dict] = {}
    if users_path.is_file():
        users = {int(u["id"]): u for u in json.loads(users_path.read_text(encoding="utf-8"))}

    tickets_path = cfg.dirs["tickets"] / "tickets.jsonl"
    if tickets_path.is_file() and "--api" not in argv:
        exported = []
        for line in tickets_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                exported.append(json.loads(line))
        live = live_from_export(exported)
        source = "export (tickets.jsonl)"
    else:
        live = tickets_from_zendesk(client)
        source = "Zendesk search API"

    live.sort(key=lambda t: str(t.get("updated_at") or ""), reverse=True)
    print(f"Live tickets from {source}: {len(live)}")
    for t in live:
        print(format_row(t, users))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
