"""Tests for the importer's pure payload / progress logic (no network)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zdmigrate.importer import (
    build_assignee_map,
    build_conversation_payload,
    chatwoot_agents_by_email,
    files_for_comment,
    load_agent_map,
    load_import_progress,
    resolve_assignee_id,
    ticket_create_status,
)
from zdmigrate.transform import map_create_status


class TestCreateStatus(unittest.TestCase):
    def test_coercion(self):
        self.assertEqual(map_create_status("open"), "open")
        self.assertEqual(map_create_status("pending"), "pending")
        self.assertEqual(map_create_status("snoozed"), "pending")
        self.assertEqual(map_create_status("resolved"), "resolved")


class TestConversationPayload(unittest.TestCase):
    def _ticket(self, **over):
        base = {
            "id": 1001,
            "status": "solved",
            "subject": "Recording issue",
            "requester_id": 20001,
            "tags": ["billing"],
            "custom_fields": [{"id": 111111, "value": "product_a"}],
        }
        base.update(over)
        return base

    def test_basic_payload(self):
        p = build_conversation_payload(
            self._ticket(), contact_id=5, source_id="a@b.c", inbox_id=7,
            field_map={111111: "category"},
        )
        self.assertEqual(p["contact_id"], 5)
        self.assertEqual(p["source_id"], "a@b.c")
        self.assertEqual(p["inbox_id"], 7)
        self.assertEqual(p["status"], "resolved")
        self.assertEqual(p["additional_attributes"]["zendesk_ticket_id"], 1001)
        self.assertEqual(p["additional_attributes"]["mail_subject"], "Recording issue")
        self.assertEqual(p["custom_attributes"], {"category": "product_a"})

    def test_empty_subject_omitted(self):
        p = build_conversation_payload(self._ticket(subject=""), 5, "a@b.c", 7)
        self.assertNotIn("mail_subject", p["additional_attributes"])

    def test_no_custom_fields_key_when_empty(self):
        p = build_conversation_payload(self._ticket(custom_fields=[]), 5, "a@b.c", 7)
        self.assertNotIn("custom_attributes", p)

    def test_open_ticket_status(self):
        p = build_conversation_payload(self._ticket(status="open"), 5, "a@b.c", 7)
        self.assertEqual(p["status"], "open")

    def test_hold_coerced_to_pending(self):
        p = build_conversation_payload(self._ticket(status="hold"), 5, "a@b.c", 7)
        self.assertEqual(p["status"], "pending")

    def test_ticket_create_status(self):
        self.assertEqual(ticket_create_status(self._ticket(status="solved")), "resolved")
        self.assertEqual(ticket_create_status(self._ticket(status="closed")), "resolved")
        self.assertEqual(ticket_create_status(self._ticket(status="open")), "open")
        self.assertEqual(ticket_create_status(self._ticket(status="pending")), "pending")


class TestProgressAndMap(unittest.TestCase):
    def test_last_line_wins(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "imported.jsonl"
            p.write_text(
                json.dumps({"zendesk_ticket_id": 1, "status": "partial", "conversation_id": 9}) + "\n"
                + json.dumps({"zendesk_ticket_id": 1, "status": "complete", "conversation_id": 9}) + "\n",
                encoding="utf-8",
            )
            done = load_import_progress(p)
            self.assertEqual(done[1]["status"], "complete")

    def test_agent_map_skips_comment(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "agent_map.json"
            p.write_text(json.dumps({"_comment": "x", "10001": 2}), encoding="utf-8")
            self.assertEqual(load_agent_map(p), {10001: 2})

    def test_files_attachments_then_inline(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "99__doc.pdf").write_bytes(b"x")
            (d / "inline_5_0.png").write_bytes(b"y")
            (d / "inline_5_1.png").write_bytes(b"z")
            comment = {"id": 5, "attachments": [{"id": 99}]}
            names = [f.name for f in files_for_comment(d, comment)]
            self.assertEqual(names[0], "99__doc.pdf")
            self.assertEqual(names[1:], ["inline_5_0.png", "inline_5_1.png"])


class TestAssigneeEmailMatch(unittest.TestCase):
    def test_match_by_email(self):
        users = {10: {"email": "Ada@Example.com", "name": "Ada"}}
        cw = chatwoot_agents_by_email([{"id": 3, "email": "ada@example.com"}])
        m, warns = build_assignee_map(users, [10], cw, {})
        self.assertEqual(m, {10: 3})
        self.assertEqual(warns, [])

    def test_override_wins(self):
        users = {10: {"email": "ada@example.com"}}
        cw = chatwoot_agents_by_email([{"id": 3, "email": "ada@example.com"}])
        m, warns = build_assignee_map(users, [10], cw, {10: 99})
        self.assertEqual(m[10], 99)
        self.assertEqual(warns, [])

    def test_missing_warns(self):
        users = {10: {"email": "gone@example.com"}, 11: {"name": "No Mail"}}
        cw = chatwoot_agents_by_email([])
        m, warns = build_assignee_map(users, [10, 11], cw, {})
        self.assertEqual(m, {})
        self.assertEqual(len(warns), 2)

    def test_resolve_skips_unmatched(self):
        self.assertIsNone(resolve_assignee_id(10, {10: {"email": "x@y.z"}}, {}, {}))
        self.assertEqual(resolve_assignee_id(10, {}, {10: 4}, {}), 4)


class TestDryRunNoWrites(unittest.TestCase):
    def test_dry_run_never_constructs_client(self):
        with patch("zdmigrate.importer.load_config") as lc:
            from zdmigrate.importer import dry_run
            import zdmigrate.importer as imp

            cfg = MagicMock()
            tmp = Path(tempfile.mkdtemp())
            for name in ("inventory", "tickets", "conversations", "attachments", "state", "logs"):
                (tmp / name).mkdir()
            cfg.dirs = {n: tmp / n for n in ("inventory", "tickets", "conversations", "attachments", "state", "logs")}
            (tmp / "inventory" / "users.json").write_text("[]", encoding="utf-8")
            (tmp / "inventory" / "agent_ids.json").write_text("[1]", encoding="utf-8")
            (tmp / "tickets" / "tickets.jsonl").write_text("", encoding="utf-8")
            lc.return_value = cfg
            self.assertFalse(hasattr(imp, "ChatwootClient"))
            self.assertEqual(dry_run(), 0)


if __name__ == "__main__":
    unittest.main()
