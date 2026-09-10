# Zendesk → Chatwoot migration (Python)

DIY tooling to export Zendesk ticket history and import it into self-hosted
Chatwoot.

Pipeline: **export → transform → import → SQL timestamp fixup**, plus verify
and a live-ticket cutover report.

## Status

| Stage | Module | State |
|-------|--------|-------|
| Export (Zendesk → disk) | `zdmigrate/export.py` | built, resumable |
| Transform (clean/map) | `zdmigrate/transform.py` | built + tested |
| Import (→ Chatwoot API) | `zdmigrate/importer.py` | built + tested; `--dry-run` |
| Timestamp fixup (SQL) | `zdmigrate/fixup.py` | built (`display_id` + `account_id`) |
| Verify | `zdmigrate/verify.py` | built |
| Live tickets | `zdmigrate/live_tickets.py` | built, read-only |

Help Center / Guide articles are **not** in this pipeline.

## Setup

```bash
cp .env.example .env      # fill in Zendesk + Chatwoot vars
pip install -r requirements.txt
```

Optionally copy `mapping/custom_field_map.json.example` → `mapping/custom_field_map.json`.
Assignees are matched to existing Chatwoot agents **by email** (from the Zendesk
user inventory). `mapping/agent_map.json` is an optional id override. Missing
Chatwoot agents are **not** created; those tickets are left unassigned and a
warning is logged. Those files (except `.example`) are gitignored.

API token: Zendesk Admin Center → Apps and integrations → APIs → Zendesk API →
enable Token access, add a token.

## Run the export

```bash
python -m zdmigrate.export            # all stages
python -m zdmigrate.export inventory  # just reference data
python -m zdmigrate.export tickets    # just the ticket list
python -m zdmigrate.export content    # just comments + attachments
```

Everything is written under `storage/` (gitignored) and is **resumable**.

```
storage/
  inventory/     brands, groups, ticket_fields, users, organizations, agent_ids
  tickets/       tickets.jsonl  (one ticket per line, full history)
  conversations/ {ticket_id}.json  (ordered comments)
  attachments/   {ticket_id}/{attachment_id}__{filename}  + inline_{...} images
  state/         resume checkpoints, imported.jsonl, timestamps.jsonl
  logs/          per-run logs
```

## Run the tests

```bash
python -m unittest discover tests
```

## Mapping decisions

- **One API inbox** — product/line can be preserved via a conversation custom
  attribute (see `custom_field_map.json`) and tags, not separate inboxes.
- **Custom fields** — only ids you list in `mapping/custom_field_map.json` are
  copied. Zendesk analytics fields can stay unmapped.
- **Direction** — `author_id` against the agent set, **not** the `side` field.
- **Quote bloat** — Zendesk reply marker and gmail-style `On <date> … wrote:`.
  Boilerplate-only comments are skipped on import.
- **Inline images** — in the message body, not `attachments[]`. Exporter
  downloads only images in the author's own content; importer attaches them on
  the same Chatwoot message (quoted-footer logos are ignored).

## Status mapping (Zendesk → Chatwoot)

| Zendesk | Conceptual Chatwoot | Sent on conversation **create** |
|---------|---------------------|----------------------------------|
| new, open | open | open |
| pending | pending | pending |
| hold (On-hold) | snoozed | **pending** (create enum has no snoozed) |
| solved, closed | resolved | resolved |

## Import into Chatwoot

### Use an API-type inbox

Create **one** inbox for the import and make it an **API** channel — *not* an
Email inbox. Creating historical **outgoing** (agent) messages in an Email inbox
would make Chatwoot **send real emails to your customers**.

Define matching conversation custom attributes in Chatwoot if you map fields.

### Mute notifications before a bulk run

The Chatwoot API has **no** flag to skip agent notifications. Creating a
conversation (and assigning it, and posting incoming messages) enqueues
email / push / in-app jobs. A bulk import **will flood agents** unless you
stop those jobs from running.

Before importing:

1. Take a database backup.
2. Pause automation rules and webhooks that would fire on new conversations.
3. **Stop Sidekiq** so notification jobs are not processed:
   - Super Admin → Sidekiq (`/super_admin`, then Sidekiq in the sidebar) → **Quiet** all processes, **or**
   - Linux VM: `sudo systemctl stop chatwoot-worker.1.service` (leave `chatwoot-web` running), **or**
   - Docker Compose: `docker compose stop sidekiq`
4. Optionally turn off each agent's Profile Settings checkboxes for
   conversation created, conversation assigned, and new message on assigned
   conversation (belt and suspenders).

After the import (and timestamp fixup):

1. In Super Admin → Sidekiq, **delete queued / retry / scheduled jobs**
   (especially the `mailers` queue). If you start the worker without this,
   every deferred notification fires at once.
2. Unquiet Sidekiq, or start the worker again
   (`sudo systemctl start chatwoot-worker.1.service` /
   `docker compose start sidekiq`).
3. Re-enable agent notification preferences if you turned them off.

### Run order

```bash
python -m zdmigrate.export

python -m zdmigrate.importer --dry-run
python -m zdmigrate.importer --limit 50
python -m zdmigrate.importer

python -m zdmigrate.verify

python -m zdmigrate.fixup
psql "$CHATWOOT_DATABASE_URL" -1 -f storage/state/timestamp_fixup.sql

python -m zdmigrate.live_tickets
```

The importer is resumable: `storage/state/imported.jsonl` records
`complete` / `partial` plus `last_comment_id` (last line wins). Chatwoot
conversation `id` in the API is **display_id**; timestamp SQL updates
`conversations` by `account_id` + `display_id` and **sets** `last_activity_at`
to the latest message time.

Merged Zendesk tickets get a private note and a `zendesk-merged` label.

## Still separate

- Channel repointing (live email, chat widget) after cutover.
- Help Center / Guide articles.
