# Cursor handoff brief — Zendesk → Chatwoot migration

Drop this at the repo root (e.g. as `AGENTS.md`) and give it to Cursor as
context before asking it to extend the project. It captures what exists, the
decisions that must not be broken, the external API contracts, and the
remaining work as concrete tasks.

---

## 1. Goal

Migrate Zendesk support history into a **self-hosted Chatwoot**, preserving
conversations, message direction, attachments, timestamps, and basic metadata.

Pipeline: **export (Zendesk → disk) → transform (clean/map) → import (→ Chatwoot
API) → SQL timestamp fixup**.

---

## 2. Repo layout

```
zdmigrate/
  config.py           Zendesk + storage config (.env loader, Config.dirs)
  chatwoot_config.py  Chatwoot connection config
  logger.py           stderr+file logger
  zendesk_client.py   Zendesk REST client: auth, throttle/retry, cursor
                      pagination, incremental ticket export, downloads
  chatwoot_client.py  Chatwoot app-API client: contacts, contact_inboxes,
                      conversations, messages (JSON + multipart), labels
  transform.py        PURE logic: quote-stripping, inline-image extraction,
                      author direction, status/custom-field mapping
  export.py           stages: inventory | tickets | content (resumable)
  importer.py         creates CW contacts/conversations/messages (idempotent)
  fixup.py            emits SQL to backdate created_at in Postgres
  verify.py           exported vs imported reconcile
  live_tickets.py     read-only non-closed ticket report
tests/
  test_transform.py   quote/inline/direction/status fixtures
  test_importer.py    payload builders + status coercion + dry-run
  test_fixup.py       display_id SQL + last_activity_at
  test_verify.py      mismatch comparison
  test_live_tickets.py
mapping/agent_map.json.example
mapping/custom_field_map.json.example
.env.example  requirements.txt  README.md
```

Only dependency is `requests`. Everything else is stdlib. Python 3.12.

---

## 3. Data flow

```
Zendesk API ──export──> storage/
                          inventory/{brands,groups,ticket_fields,users,
                                     organizations,agent_ids}.json
                          tickets/tickets.jsonl              (one ticket/line)
                          conversations/{ticket_id}.json     (ordered comments)
                          attachments/{ticket_id}/...        (+ inline images)
                          state/                             (resume checkpoints)

storage/ ──importer──> Chatwoot (contacts, conversations, messages, labels)
                        + state/imported.jsonl   (zendesk_ticket_id→conv_id)
                        + state/timestamps.jsonl (kind,id,created_at)

state/timestamps.jsonl ──fixup──> state/timestamp_fixup.sql ──psql──> Postgres
```

---

## 4. Invariants — do NOT break these

These were derived from the live data and the Chatwoot API; changing them
silently reintroduces real bugs.

- **Import target must be an API-type Chatwoot inbox.** Creating outgoing (agent)
  messages in an **Email** inbox makes Chatwoot send real emails to customers.
  Prefer one inbox; product/line can be preserved via custom attributes + tags,
  not separate inboxes.
- **Timestamps can't be set via the API** (server stamps "now"). They are fixed
  afterward by `fixup.py` → SQL `UPDATE` on `messages.created_at` /
  `conversations.created_at`. Keep recording `state/timestamps.jsonl` during
  import.
- **Message direction is decided by author id ∈ agent set**, NOT Zendesk's
  `side` field (which mislabels email round-trips). Agent ids come from
  `inventory/agent_ids.json`.
- **Quote-stripping** removes replayed email history via two markers: the
  Zendesk reply marker (`##- please type your reply above this line -##`) and
  gmail-style `On <date> … wrote:`. Boilerplate-only comments collapse to empty
  and are skipped on import.
- **Inline images** live in the message body (`[Image: alt](url)`), not
  `attachments[]`, and are hosted on the zendesk domain (they rot at
  cancellation). Only images in the author's own (un-quoted) content are taken.
- **Idempotency & resumability:** export checkpoints a cursor + a done-set;
  import records each ticket in `state/imported.jsonl` and skips it on re-run.
  Any new long-running step must be re-runnable the same way.
- **Custom fields** only migrate when listed in `mapping/custom_field_map.json`.
  Do not assume a brand→inbox split unless the source account requires it.

---

## 5. External API contracts (already used)

**Zendesk** (`GET`, api-token basic auth `{email}/token:{token}`):
- `incremental/tickets/cursor.json?start_time=0` → tickets + `after_url` +
  `end_of_stream` (full history)
- `tickets/{id}/comments.json` (cursor-paginated) → comments with
  `author_id`, `public`, `created_at`, body fields, `attachments[]`
- `brands|groups|ticket_fields|users|organizations.json` (cursor-paginated)
- attachment `content_url` → binary

**Chatwoot** application API (`api_access_token` and `api-access-token` headers, admin token), base
`{CHATWOOT_URL}/api/v1/accounts/{account_id}/`:
- `contacts/search?q=` ; `POST contacts` ; `POST contacts/{id}/contact_inboxes`
- `POST conversations` — payload: `source_id` (req), `inbox_id`, `contact_id`,
  `status` ∈ {open,pending,resolved}, `additional_attributes`
  (we store `zendesk_ticket_id`, `mail_subject`), `custom_attributes`,
  `assignee_id`
- `POST conversations/{id}/messages` — `content`, `message_type`
  (incoming|outgoing), `private`; multipart `attachments[]` for files
- `POST conversations/{id}/labels` — `{labels:[...]}`

Docs index: https://developers.chatwoot.com/llms.txt (fetch specific pages as
needed; prefer the **application** API, not the public one).

---

## 6. Conventions

- Deps: stdlib + `requests` only. Keep transform logic pure and unit-tested.
- Clients: throttle + retry with `Retry-After`, backoff on 429/5xx.
- Long jobs: write to disk first, checkpoint, be idempotent.
- Config via `.env`; never hardcode secrets. Run modules with `python -m
  zdmigrate.<module>`.
- Tests: `python -m unittest discover tests`.

---

## 7. Remaining work

### T1–T3, T5 — done
Verify (`python -m zdmigrate.verify`), live tickets (`python -m zdmigrate.live_tickets`),
importer `--dry-run`, and inline images attached on the same message (quoted logos ignored).

### T4 — Help Center / Guide article migration (separate track)
Export Zendesk Guide articles (categories → sections → articles, HTML bodies +
images) and create them in Chatwoot's Help Center via its API. New module
`zdmigrate/kb.py`; keep it independent of the ticket pipeline.
- **Acceptance:** article count and section structure match; images rehosted;
  idempotent by source article id.
