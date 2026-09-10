"""Pure verify comparison: missing tickets, message gaps, no false positive on empty."""
import unittest

from zdmigrate.verify import compare


AGENTS = {10}


class TestCompare(unittest.TestCase):
    def test_not_imported_listed(self):
        tickets = [{"id": 1}, {"id": 2}]
        progress = {
            1: {"status": "complete", "conversation_id": 9, "message_count": 0, "file_count": 0},
        }
        comments = {1: [], 2: []}
        mismatches, summary = compare(tickets, progress, comments, att_root=__import__("pathlib").Path("."), agent_ids=AGENTS)
        issues = {(m.ticket_id, m.issue) for m in mismatches}
        self.assertIn((2, "not_imported"), issues)
        self.assertEqual(summary["imported_complete"], 1)
        self.assertEqual(summary["not_imported"], 1)

    def test_boilerplate_not_a_gap(self):
        tickets = [{"id": 5}]
        comments = {
            5: [{
                "id": 1,
                "author_id": 99,
                "public": True,
                "plain_body": "##- Please type your reply above this line -##\nDelivered by Zendesk",
            }],
        }
        progress = {
            5: {"status": "complete", "conversation_id": 1, "message_count": 0, "file_count": 0},
        }
        mismatches, _ = compare(tickets, progress, comments, __import__("pathlib").Path("/nope"), AGENTS)
        self.assertEqual([m for m in mismatches if m.issue == "message_count"], [])

    def test_message_gap(self):
        tickets = [{"id": 7}]
        comments = {
            7: [{"id": 1, "author_id": 99, "public": True, "plain_body": "hello"}],
        }
        progress = {
            7: {"status": "complete", "conversation_id": 1, "message_count": 0, "file_count": 0},
        }
        mismatches, _ = compare(tickets, progress, comments, __import__("pathlib").Path("/nope"), AGENTS)
        self.assertTrue(any(m.issue == "message_count" for m in mismatches))


if __name__ == "__main__":
    unittest.main()
