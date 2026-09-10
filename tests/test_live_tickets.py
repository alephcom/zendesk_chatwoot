"""Live-ticket filter (no Zendesk writes)."""
import unittest

from zdmigrate.live_tickets import live_from_export


class TestLiveFromExport(unittest.TestCase):
    def test_excludes_closed(self):
        tickets = [
            {"id": 1, "status": "closed"},
            {"id": 2, "status": "open"},
            {"id": 3, "status": "hold"},
            {"id": 4, "status": "solved"},
            {"id": 5, "status": "pending"},
        ]
        live = live_from_export(tickets)
        self.assertEqual({t["id"] for t in live}, {2, 3, 4, 5})


if __name__ == "__main__":
    unittest.main()
