"""Compare exported Zendesk data against import progress.

Run: python -m zdmigrate.verify
Writes storage/state/verify_report.csv and a console summary.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import load_config
from .importer import files_for_comment, load_import_progress
from .transform import clean_comment, expected_message_count, find_merge_target


@dataclass
class TicketMismatch:
    ticket_id: int
    issue: str
    expected: str
    actual: str


def expected_file_count(comments: list[dict], att_dir: Path, agent_ids: set[int]) -> int:
    n = 0
    for comment in comments:
        msg = clean_comment(comment, agent_ids)
        files = files_for_comment(att_dir, comment)
        if msg.is_empty and not files:
            continue
        n += len(files)
    return n


def compare(
    tickets: list[dict],
    progress: dict[int, dict],
    comments_by_id: dict[int, list[dict]],
    att_root: Path,
    agent_ids: set[int],
) -> tuple[list[TicketMismatch], dict]:
    mismatches: list[TicketMismatch] = []
    exported = len(tickets)
    complete = 0
    partial = 0
    missing = 0

    for ticket in tickets:
        tid = int(ticket.get("id") or 0)
        if not tid:
            continue
        rec = progress.get(tid)
        comments = comments_by_id.get(tid, [])
        att_dir = att_root / str(tid)
        exp_msgs = expected_message_count(comments, agent_ids)
        if find_merge_target(ticket, comments):
            exp_msgs += 1
        exp_files = expected_file_count(comments, att_dir, agent_ids)

        if rec is None:
            missing += 1
            mismatches.append(TicketMismatch(tid, "not_imported", "complete", "missing"))
            continue

        status = rec.get("status") or ""
        if status == "complete":
            complete += 1
        elif status == "partial":
            partial += 1
            mismatches.append(TicketMismatch(tid, "partial_import", "complete", "partial"))
        else:
            missing += 1
            mismatches.append(TicketMismatch(tid, "not_imported", "complete", status or "missing"))

        actual_msgs = int(rec.get("message_count") or 0)
        if status == "complete" and actual_msgs != exp_msgs:
            mismatches.append(TicketMismatch(
                tid, "message_count", str(exp_msgs), str(actual_msgs),
            ))
        actual_files = int(rec.get("file_count") or 0)
        if status == "complete" and actual_files != exp_files:
            mismatches.append(TicketMismatch(
                tid, "attachment_count", str(exp_files), str(actual_files),
            ))
        if rec.get("conversation_id") in (None, "", 0):
            mismatches.append(TicketMismatch(tid, "missing_conversation", "id", "none"))

    summary = {
        "exported": exported,
        "imported_complete": complete,
        "imported_partial": partial,
        "not_imported": missing,
        "mismatch_rows": len(mismatches),
    }
    return mismatches, summary


def load_tickets(path: Path) -> list[dict]:
    tickets = []
    if not path.is_file():
        return tickets
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tickets.append(json.loads(line))
    return tickets


def load_comments(conv_dir: Path, ticket_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for tid in ticket_ids:
        p = conv_dir / f"{tid}.json"
        if p.is_file():
            out[tid] = json.loads(p.read_text(encoding="utf-8"))
        else:
            out[tid] = []
    return out


def load_agent_ids(path: Path) -> set[int]:
    if not path.is_file():
        return set()
    return {int(x) for x in json.loads(path.read_text(encoding="utf-8"))}


def run() -> int:
    cfg = load_config()
    tickets_path = cfg.dirs["tickets"] / "tickets.jsonl"
    if not tickets_path.is_file():
        sys.exit("Missing tickets.jsonl — run the export first.")
    tickets = load_tickets(tickets_path)
    ids = [int(t["id"]) for t in tickets if t.get("id")]
    comments = load_comments(cfg.dirs["conversations"], ids)
    progress = load_import_progress(cfg.dirs["state"] / "imported.jsonl")
    agent_ids = load_agent_ids(cfg.dirs["inventory"] / "agent_ids.json")
    mismatches, summary = compare(
        tickets, progress, comments, cfg.dirs["attachments"], agent_ids,
    )

    report = cfg.dirs["state"] / "verify_report.csv"
    with open(report, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticket_id", "issue", "expected", "actual"])
        w.writeheader()
        for row in mismatches:
            w.writerow(asdict(row))

    print(
        f"exported={summary['exported']} complete={summary['imported_complete']} "
        f"partial={summary['imported_partial']} not_imported={summary['not_imported']} "
        f"mismatch_rows={summary['mismatch_rows']}"
    )
    print(f"Wrote {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
