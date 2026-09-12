"""Inbox repoint SQL uses display_id and does not mix API/email inbox ids."""
import json
import tempfile
import unittest
from pathlib import Path

from zdmigrate.repoint import generate_sql, imported_display_ids


class TestImportedDisplayIds(unittest.TestCase):
    def test_complete_only(self):
        progress = {
            1: {"status": "complete", "conversation_id": 10},
            2: {"status": "partial", "conversation_id": 11},
            3: {"status": "complete", "conversation_id": 12},
            4: {"status": "complete"},
        }
        self.assertEqual(imported_display_ids(progress), [10, 12])

    def test_from_jsonl_last_line_wins(self):
        from zdmigrate.importer import load_import_progress

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "imported.jsonl"
            p.write_text(
                json.dumps({"zendesk_ticket_id": 1, "status": "partial", "conversation_id": 9})
                + "\n"
                + json.dumps({"zendesk_ticket_id": 1, "status": "complete", "conversation_id": 9})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(imported_display_ids(load_import_progress(p)), [9])


class TestGenerateSql(unittest.TestCase):
    def test_sql_targets_display_id_and_inboxes(self):
        sql = generate_sql([12, 15], account_id=1, api_inbox_id=4, email_inbox_id=2)
        self.assertIn("CREATE TEMP TABLE zd_repoint_display_ids", sql)
        self.assertIn("  (12)", sql)
        self.assertIn("  (15)", sql)
        self.assertIn("c.inbox_id = 4", sql)
        self.assertIn("SET inbox_id = 2", sql)
        self.assertIn("e.inbox_id = 2", sql)
        self.assertIn("WHERE c.account_id = 1", sql)
        self.assertNotIn("display_id IN", sql)
        self.assertIn("m.conversation_id IN", sql)
        self.assertIn("SELECT c.id FROM conversations c", sql)

    def test_rejects_same_inbox(self):
        with self.assertRaises(ValueError):
            generate_sql([1], account_id=1, api_inbox_id=4, email_inbox_id=4)


if __name__ == "__main__":
    unittest.main()
