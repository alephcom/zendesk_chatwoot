"""SQL timestamp fixup uses display_id + account_id; SET last_activity_at."""
import unittest

from zdmigrate.fixup import generate_sql


class TestGenerateSql(unittest.TestCase):
    def test_display_id_not_pk(self):
        records = [
            {"kind": "conversation", "display_id": 12, "created_at": "2020-01-01T00:00:00Z"},
            {"kind": "message", "id": 55, "display_id": 12, "created_at": "2020-01-02T00:00:00Z"},
        ]
        sql = generate_sql(records, account_id=3)
        self.assertIn("WHERE account_id = 3 AND display_id = 12", sql)
        self.assertNotIn("WHERE id = 12", sql)
        self.assertIn("UPDATE messages SET created_at = '2020-01-02T00:00:00Z' WHERE id = 55", sql)
        self.assertIn("last_activity_at = '2020-01-02T00:00:00Z'", sql)
        self.assertNotIn("GREATEST", sql)

    def test_legacy_id_on_conversation(self):
        records = [
            {"kind": "conversation", "id": 8, "created_at": "2019-05-05T12:00:00Z"},
        ]
        sql = generate_sql(records, account_id=1)
        self.assertIn("display_id = 8", sql)
        self.assertIn("last_activity_at = '2019-05-05T12:00:00Z'", sql)


if __name__ == "__main__":
    unittest.main()
