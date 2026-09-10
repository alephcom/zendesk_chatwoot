"""Transform Zendesk comments into Chatwoot-shaped messages.

Typical Zendesk email tickets:
  - email-quote bloat appears via a Zendesk reply marker AND gmail-style
    "On <date> ... wrote:" quoting, even on natively-handled tickets;
  - inline images live in the message body (as `[Image: alt](url)`), NOT in
    the comment's attachments[] array, and are hosted on the zendesk domain
    (so they die when Zendesk is cancelled);
  - the `side` field is unreliable on email round-trips, so message direction
    is decided by matching author_id against the known agent set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# --- Zendesk -> Chatwoot status mapping (from list_ticket_fields) ----------
# Zendesk status category -> Chatwoot conversation status.
STATUS_MAP: dict[str, str] = {
    "new": "open",
    "open": "open",
    "pending": "pending",
    "hold": "snoozed",      # "On-hold" custom status
    "solved": "resolved",
    "closed": "resolved",
}

# Zendesk custom field id -> Chatwoot conversation custom attribute name.
# Site-specific ids live in mapping/custom_field_map.json (see the .example).
CUSTOM_FIELD_MAP: dict[int, str] = {}

# --- quote / signature markers --------------------------------------------
# Everything from this Zendesk reply marker downward is quoted history.
REPLY_MARKER = re.compile(r"#{2,}-.*type your reply above this line.*-#{2,}", re.IGNORECASE)
# Gmail / Apple Mail style attribution line that precedes a quoted block.
ON_WROTE = re.compile(r"^\s*On .{0,200}?\bwrote:\s*$", re.IGNORECASE | re.MULTILINE)
# Inline image placeholder rendered in the body: [Image: alt](https://...)
INLINE_IMG = re.compile(r"\[Image:[^\]]*\]\((https?://[^)\s]+)\)")
# Collapse 3+ blank lines to a single blank line.
MULTI_BLANK = re.compile(r"\n{3,}")
# Zendesk merge notice in comment bodies.
MERGE_INTO = re.compile(r"merged into request\s+#?\s*(\d+)", re.IGNORECASE)


@dataclass
class CleanMessage:
    content: str
    message_type: str          # "incoming" | "outgoing"
    private: bool              # internal note
    created_at: str           # original Zendesk timestamp (ISO 8601)
    author_id: int
    inline_image_urls: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing but boilerplate/quotes remained after cleaning."""
        return not self.content.strip() and not self.attachments and not self.inline_image_urls


def _kept_region(text: str) -> str:
    """The author's own content: everything before the first quote boundary."""
    if not text:
        return ""
    cut = len(text)
    m = REPLY_MARKER.search(text)
    if m:
        cut = min(cut, m.start())
    m = ON_WROTE.search(text)
    if m:
        cut = min(cut, m.start())
    return text[:cut]


def inline_images_from_body(text: str) -> list[str]:
    """Inline image URLs found in the author's own content only.

    Deliberately ignores images inside quoted history/footers (e.g. repeated
    company-logo images in email signatures), so they aren't rehosted thousands
    of times.
    """
    return INLINE_IMG.findall(_kept_region(text))


def strip_quoted(text: str) -> str:
    """Remove replayed email history and collapse whitespace.

    Cuts at the first quote boundary found (Zendesk reply marker or an
    "On ... wrote:" attribution line), keeping only the author's new content.
    """
    kept = _kept_region(text)
    # drop inline-image placeholders from the visible text (URLs captured separately)
    kept = INLINE_IMG.sub("", kept)
    kept = MULTI_BLANK.sub("\n\n", kept)
    # normalise non-breaking spaces that Zendesk/email inject
    kept = kept.replace("\u00a0", " ")
    return kept.strip()


def classify_direction(author_id: int, agent_ids: Iterable[int]) -> str:
    """outgoing if the author is one of our agents, else incoming.

    More reliable than the Zendesk `side` field, which mislabels email
    round-trips (confirmed on real merged/emailed tickets).
    """
    return "outgoing" if author_id in set(agent_ids) else "incoming"


def clean_comment(comment: dict, agent_ids: Iterable[int]) -> CleanMessage:
    """Turn one Zendesk comment into a Chatwoot-ready message."""
    # prefer plain_body, fall back to body/text (raw API gives plain_body/html_body;
    # the field name differs by source, so accept several).
    raw = comment.get("plain_body") or comment.get("body") or comment.get("text") or ""
    author_id = int(comment.get("author_id") or 0)
    return CleanMessage(
        content=strip_quoted(raw),
        message_type=classify_direction(author_id, agent_ids),
        private=not comment.get("public", True),
        created_at=comment.get("created_at") or comment.get("timestamp") or "",
        author_id=author_id,
        inline_image_urls=inline_images_from_body(raw),
        attachments=list(comment.get("attachments") or []),
    )


def map_status(zendesk_status: str) -> str:
    return STATUS_MAP.get((zendesk_status or "").lower(), "resolved")


# Chatwoot's conversation-create endpoint only accepts these three statuses.
# "snoozed" is a real Chatwoot state but is set via snoozed_until/toggle, not
# create — so coerce it to pending at import time.
CREATE_STATUS_MAP: dict[str, str] = {
    "open": "open",
    "pending": "pending",
    "snoozed": "pending",
    "resolved": "resolved",
}


def map_create_status(chatwoot_status: str) -> str:
    return CREATE_STATUS_MAP.get(chatwoot_status, "resolved")


def map_custom_fields(
    custom_fields: list[dict],
    field_map: dict[int, str] | None = None,
) -> dict[str, str]:
    """Map Zendesk custom_fields [{id, value}] -> {chatwoot_attribute: value}."""
    mapping = field_map if field_map is not None else CUSTOM_FIELD_MAP
    out: dict[str, str] = {}
    for cf in custom_fields or []:
        name = mapping.get(int(cf.get("id", 0)))
        if name and cf.get("value") not in (None, ""):
            out[name] = str(cf["value"])
    return out


def find_merge_target(ticket: dict, comments: list[dict] | None = None) -> int | None:
    """Return the Zendesk ticket id this ticket was merged into, if any."""
    via = ticket.get("via") or {}
    source = via.get("source") or {}
    rel = str(via.get("rel") or source.get("rel") or "").lower()
    if rel == "merge":
        to_id = (source.get("to") or {}).get("ticket_id") or (source.get("from") or {}).get("ticket_id")
        if to_id:
            try:
                return int(to_id)
            except (TypeError, ValueError):
                pass
    for comment in comments or []:
        raw = comment.get("plain_body") or comment.get("body") or comment.get("text") or ""
        m = MERGE_INTO.search(raw)
        if m:
            return int(m.group(1))
    return None


def api_message_content(content: str, has_files: bool) -> str:
    """Chatwoot requires content; attachment-only comments need a placeholder."""
    text = (content or "").strip()
    if text:
        return text
    if has_files:
        return "(attachment)"
    return ""


def should_import_comment(comment: dict, agent_ids: Iterable[int]) -> bool:
    """True if this comment should become a Chatwoot message (not boilerplate-only)."""
    msg = clean_comment(comment, agent_ids)
    return not msg.is_empty


def expected_message_count(comments: list[dict], agent_ids: Iterable[int]) -> int:
    n = 0
    for c in comments or []:
        if should_import_comment(c, agent_ids):
            n += 1
    return n
