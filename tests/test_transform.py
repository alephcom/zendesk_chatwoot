"""Transform tests: quote stripping, direction, category, inline images, merges."""
from __future__ import annotations

import unittest

from zdmigrate.transform import (
    api_message_content,
    classify_direction,
    clean_comment,
    expected_message_count,
    find_merge_target,
    inline_images_from_body,
    map_create_status,
    map_custom_fields,
    map_status,
    strip_quoted,
)

AGENT = 10001
CUSTOMER = 20001
AGENTS = {AGENT}
CATEGORY_FIELD = 111111

INLINE_BODY = """Hi, the recorder dropped frames.

[Image: screenshot](https://example.zendesk.com/attachments/token/abc/screenshot.png)

##- Please type your reply above this line -##

On Mon, 1 Jan 2024 at 10:00 Customer wrote:

[Image: company logo](https://example.zendesk.com/attachments/token/logo/logo.png)
Delivered by Zendesk
"""

NBSP_BODY = """Thanks, that fixed it.\u00a0See you.

On Tue, Jan 2, 2024 at 9:00 AM Support wrote:
> previous thread
"""


class TestQuoteStrip(unittest.TestCase):
    def test_zendesk_marker(self):
        text = "Hello\n\n##- Please type your reply above this line -##\n\nquoted"
        self.assertEqual(strip_quoted(text), "Hello")

    def test_on_wrote(self):
        self.assertEqual(strip_quoted(NBSP_BODY), "Thanks, that fixed it. See you.")

    def test_nbsp_normalised(self):
        self.assertNotIn("\u00a0", strip_quoted(NBSP_BODY))

    def test_boilerplate_empty(self):
        body = "##- Please type your reply above this line -##\nDelivered by Zendesk"
        msg = clean_comment({"plain_body": body, "author_id": CUSTOMER, "public": True}, AGENTS)
        self.assertTrue(msg.is_empty)


class TestInlineImages(unittest.TestCase):
    def test_unquoted_kept_quoted_logo_ignored(self):
        urls = inline_images_from_body(INLINE_BODY)
        self.assertEqual(
            urls,
            ["https://example.zendesk.com/attachments/token/abc/screenshot.png"],
        )
        cleaned = strip_quoted(INLINE_BODY)
        self.assertNotIn("screenshot.png", cleaned)
        self.assertNotIn("logo.png", cleaned)
        self.assertIn("recorder dropped frames", cleaned)

    def test_quoted_only_image_not_extracted(self):
        body = "ok\n\n##- Please type your reply above this line -##\n[Image: logo](https://x/logo.png)"
        self.assertEqual(inline_images_from_body(body), [])


class TestDirection(unittest.TestCase):
    def test_agent_outgoing(self):
        self.assertEqual(classify_direction(AGENT, AGENTS), "outgoing")

    def test_customer_incoming(self):
        self.assertEqual(classify_direction(CUSTOMER, AGENTS), "incoming")

    def test_ignores_side_field(self):
        msg = clean_comment(
            {"plain_body": "hi", "author_id": AGENT, "public": True, "via": {"channel": "email"}},
            AGENTS,
        )
        self.assertEqual(msg.message_type, "outgoing")


class TestStatusAndFields(unittest.TestCase):
    def test_hold_create_pending(self):
        self.assertEqual(map_status("hold"), "snoozed")
        self.assertEqual(map_create_status(map_status("hold")), "pending")

    def test_closed_resolved(self):
        self.assertEqual(map_create_status(map_status("closed")), "resolved")

    def test_category_only(self):
        fields = [
            {"id": CATEGORY_FIELD, "value": "product_a"},
            {"id": 999999, "value": "skip-me"},
        ]
        self.assertEqual(
            map_custom_fields(fields, {CATEGORY_FIELD: "category"}),
            {"category": "product_a"},
        )


class TestMergeAndCounts(unittest.TestCase):
    def test_merge_from_body(self):
        comments = [{"plain_body": "This request was closed and merged into request #99."}]
        self.assertEqual(find_merge_target({}, comments), 99)

    def test_expected_skips_empty(self):
        comments = [
            {"id": 1, "plain_body": "real", "author_id": CUSTOMER, "public": True},
            {"id": 2, "plain_body": "##- Please type your reply above this line -##", "author_id": CUSTOMER, "public": True},
        ]
        self.assertEqual(expected_message_count(comments, AGENTS), 1)

    def test_attachment_placeholder(self):
        self.assertEqual(api_message_content("", True), "(attachment)")
        self.assertEqual(api_message_content(" hi ", False), "hi")
        self.assertEqual(api_message_content("", False), "")


if __name__ == "__main__":
    unittest.main()
