# Private iCloud MCP (`pv-icloud`)

[![CI](https://github.com/HappyCynic/pv-icloud/actions/workflows/image.yml/badge.svg?branch=main)](https://github.com/HappyCynic/pv-icloud/actions/workflows/image.yml)
[![Release](https://img.shields.io/github/v/release/HappyCynic/pv-icloud?sort=semver)](https://github.com/HappyCynic/pv-icloud/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server, **read-only by default**,
that lets MCP clients (Claude Code, Claude Desktop, …) search and read **iCloud Mail**,
**Calendar** and **Contacts**. One server can serve several iCloud accounts; each bearer token
maps to exactly one account. Moving, archiving and deleting mail can be switched on per
deployment and granted per token ([Mail writes](#mail-writes-opt-in)), and so can creating,
changing and deleting calendar events ([Calendar writes](#calendar-writes-opt-in)) and
contacts ([Contact writes](#contact-writes-opt-in)).

> **Status:** early. Mail, calendar and contacts tools work against real iCloud. Interfaces may change
> before 1.0.
>
> Not affiliated with or endorsed by Apple. iCloud is a trademark of Apple Inc.

## What it does

| Tool | Purpose |
|---|---|
| `mail_folders` | Folders with message counts |
| `mail_search` | Full-text and field search (`query`, `from`, `to`, `subject`, `folders`, `since`, `before`) across folders, newest first; paged |
| `mail_get` | One message: headers, plain-text body (HTML converted; remote content never fetched), attachment metadata; long bodies are paged |
| `mail_thread` | All messages in a conversation across folders, by `Message-ID` / `In-Reply-To` / `References`; paged |
| `calendar_list` | Calendars (name, colour, shared or subscribed) and the account's default time zone |
| `calendar_events` | Events in a date range (default: next 7 days, at most 90), recurring events expanded, filterable by calendar and text; times in each event's own zone; paged |
| `calendar_search` | Text search across all calendars over up to two years: title, location, notes, calendar name, people; paged |
| `calendar_event` | One event occurrence by `id`: every attendee and the full notes (paged by characters) |
| `find_person` | "Who is X?" in one call: matching contacts plus people seen in mail, each with how much mail you exchange, first/last dates and the newest messages; paged |
| `contacts_search` | Contacts by name, company, title, email, phone (any format), address, related names or note, optionally within a group; paged |
| `contacts_get` | One contact: names, emails, phones and addresses with labels, birthday, dates, related names, social profiles, groups, note |
| `contacts_groups` | Contact groups with member counts |

Unless [mail writes](#mail-writes-opt-in), [calendar writes](#calendar-writes-opt-in) or
[contact writes](#contact-writes-opt-in) are switched on and granted, there are **no** tools
that send, move, delete or flag mail, change events or change contacts. The IMAP read path never issues write commands (folders are opened
with `EXAMINE` and messages fetched with `BODY.PEEK`, so read state never changes), and the
CalDAV and CardDAV read clients can only send `PROPFIND` and `REPORT`.

### Mail writes (opt-in)

Off unless the deployment sets `PV_ICLOUD_MAIL_WRITE=enabled` **and** a token lists the
action in `mail_write` ([ADR-0004](docs/adr/0004-opt-in-mail-write.md)). Each action is
granted on its own; a token sees only the tools it was granted, and a token without
`mail_write` sees exactly the read-only server.

| Tool | Grant | Purpose |
|---|---|---|
| `mail_move` | `move` | Move one message to another in-scope folder |
| `mail_archive` | `archive` | Move one message to the archive folder (`Archive`, found by name) |
| `mail_delete` | `delete` | Move one message to `Deleted Messages`; it stays recoverable there until iCloud empties that folder. Nothing is ever deleted permanently. |
| `mail_draft` | `draft` | Save one plain-text draft in `Drafts` (found by name, even when hidden from reads) for a person to review and send from their mail app. Nothing is ever sent. |
| `mail_send` | `send` | Send one plain-text message from `mail.write.send_from` through iCloud SMTP, and keep one copy in `Sent Messages`. **Cannot be undone.** |

- One message per call; a move's result has the message's `new_id`.
- **Drafts:** optional `to`, `cc`, `bcc`, `subject`, `body` and `in_reply_to` (a message id,
  which threads the draft as a reply and gives it a `Re:` subject; the original stays unread).
  At most 50 addresses across `to`, `cc` and `bcc`, a subject of at most 998 characters with
  no line breaks, and a body of at most 100,000 characters. The draft is saved with
  `APPEND` flagged `\Draft \Seen`, and its `From` is `mail.write.send_from` when set (Mail
  fills in the default sender otherwise). `draft` does not grant sending; protected folders
  do not apply to drafts.
- **Sending:** the same inputs and limits as drafts, with at least one recipient.
  `mail.write.send_from` is required for an account with a `send` token (use the account's
  iCloud address or one of its aliases). The server connects to `smtp.mail.me.com:587` only
  for a `mail_send` call; it requires STARTTLS with a verified certificate before logging in,
  and gives up after 30 seconds. `bcc` addresses go only into the SMTP envelope, never into
  the message the other recipients get. After sending, the server looks for the message in
  `Sent Messages` by its Message-ID and appends one copy (flagged `\Seen`, with the `Bcc`
  header) only if iCloud has not filed one. Nothing is retried: if the connection drops
  while the message is being sent, the call says it may have been sent, to check Sent
  Messages first. `send` does not grant drafting, and protected folders do not apply.
- **`send_to`** (per token, optional): the only recipients this token may send to, as exact
  addresses or `@domain` entries (`@example.org` matches that domain only, not
  `mail.example.org`; case is ignored). If any `to`, `cc` or `bcc` address is not on the
  list, nothing is read or sent, and the error names the refused addresses. Replying does not
  add the original sender. Without `send_to` the token may send to anyone, and the server
  logs a warning naming the token at startup.

  > **Prefer `draft` over `send`.** A client that can send can be tricked by text in a
  > message into sending mail you did not intend, and sent mail cannot be recalled. If a
  > client must send, give its token a `send_to` list and a client that confirms every
  > message with you.
- **Guarded move:** iCloud has no IMAP `MOVE`, so the message is copied (`UID COPY`), and
  only once the server confirms that copy (`COPYUID`) is the original marked `\Deleted` and
  expunged, by its UID alone. If a step fails, the error says where the message is; it is
  never lost. Read state and flags are kept.
- **Protected folders** (`mail.write.protected_folders` in the defaults and per account, plus
  a token's own `protected_folders`): nothing moves into or out of them.
- **Write limit** per token: `max_writes_per_hour` (default 30), counted in memory; a restart
  resets it.
- Write calls are audited with `"write": true` and no folder names, addresses, subjects or
  bodies.

Prompt injection in mail can now cause actions, not just reads. Grant writes only to clients
that confirm actions with a person; see [SECURITY.md](SECURITY.md).

### Calendar writes (opt-in)

Off unless the deployment sets `PV_ICLOUD_CALENDAR_WRITE=enabled` **and** a token has
`calendar_write: true` ([ADR-0005](docs/adr/0005-opt-in-calendar-write.md)). The two switches
are independent of the mail ones; a token without `calendar_write` sees exactly the tools it
saw before.

| Tool | Purpose |
|---|---|
| `calendar_event_create` | Create one event, single or recurring: title, start and end (timed, or all-day dates), time zone, location, notes, display alerts, and a simple `recurrence` (daily, weekly on chosen days, monthly or yearly; `interval`; `count` or `until`) |
| `calendar_event_update` | Change the given fields of one event, or of **one occurrence** of a series (the others stay as they are); `clear` removes location, notes or alerts |
| `calendar_event_delete` | Delete one event, or one occurrence of a series. **Cannot be undone** by pv-icloud. |

- **Which calendars:** only in-scope calendars of the account that it may change: its own,
  and calendars shared with it read-write (a change there is seen by everyone sharing it).
  Subscribed calendars, calendars shared read-only and protected calendars stay read-only:
  a write to them fails with `unknown calendar`, exactly like a calendar that does not exist.
- **Protected calendars** (`calendar.write.protected_calendars` in the defaults and per
  account, plus a token's own `protected_calendars`; exact display names): readable, never
  written.
- **No attendees, no email.** iCloud sends invitations, updates and cancellations itself for
  any event with attendees, and ignores the standard way to ask it not to. So these tools
  take no attendees, and they refuse to change or delete an event that has attendees other
  than the account, or that someone else organised: change those in Calendar. Every write
  also sends `Schedule-Reply: F`.
- **Safe with other devices:** before every change or delete the event is read again from
  iCloud, and the write is conditional on it not having changed since (`If-Match`). If it
  changed in Calendar (or an attendee's reply changed it on the server) in between, the call
  fails with `the event changed in Calendar; read it again`, and the other change is kept.
  Nothing is retried.
- **Times:** ISO datetimes in an IANA time zone (default: the account's, as `calendar_list`
  shows), written with that zone; all-day events take dates with an inclusive last day, as
  the read tools return them.
- **Limits:** one event per call; title up to 255 characters, location 1000, notes 10,000; at
  most 5 alerts; an event lasts at most 366 days. The token's `max_writes_per_hour` counts
  mail and calendar writes together.
- Write calls are audited with `"write": true` and no event content: no title, notes,
  location, times, time zone or calendar name.

Out of scope: attendees, moving an event to another calendar, editing or deleting a whole
series at once ("this and following"), creating or deleting calendars, and Reminders.

### Contact writes (opt-in)

Off unless the deployment sets `PV_ICLOUD_CONTACTS_WRITE=enabled` **and** a token lists the
action in `contacts_write` ([ADR-0006](docs/adr/0006-opt-in-contact-write.md)). Each action
is granted on its own, because a delete is permanent; the switch is independent of the mail
and calendar ones, and a token without `contacts_write` sees exactly the tools it saw before.

| Tool | Grant | Purpose |
|---|---|---|
| `contacts_create` | `create` | Create one contact with any field `contacts_get` shows (names, company, title, emails, phones, addresses, URLs, social profiles, instant messaging, birthday, dates, related names, note), with labels, optionally in existing groups |
| `contacts_update` | `update` | Change, add or remove individual fields of one contact, and add it to or remove it from existing groups |
| `contacts_delete` | `delete` | Delete one contact. **Permanent:** iCloud keeps no per-contact trash, and the delete reaches every device. |

- **Lossless updates.** The stored vCard is edited in place: only the lines a call changes
  are rewritten, and every other line (the photo, Apple's labels, related names, unknown
  properties) stays byte for byte. Two more lines change on their own, as Apple's apps do:
  `REV` (the revision time), and the display name when a name part changes, unless it was a
  custom one. Anything that cannot be applied unambiguously (say, a card with two notes when
  the call changes the note) is refused with `cannot change this contact safely` and nothing
  is written.
- **Changing a value:** `remove` takes the value as `contacts_get` returned it, `add` the new
  one, in the same call.
- **Labels** are written as Apple Contacts writes them (`TEL;TYPE=CELL;TYPE=VOICE` for
  `mobile`, an item group with `X-ABLabel` for custom labels), so they read back the same.
- **Groups** are changed by editing the group's card, losslessly; groups are never created,
  renamed or deleted. Contacts in `contacts.exclude_groups` cannot be read, so they cannot be
  written either (`contact not found`), and an excluded group gives `unknown group`, like a
  missing one. The owner's own card (the me-card) can be updated but never deleted.
- **Safe with other devices:** before every update and delete the card (and any group card the
  call changes) is read again, and the write is conditional on it not having changed since
  (`If-Match`). If it changed in Contacts in between, the call fails with `the contact changed
  in Contacts; read it again`. Group changes are separate writes after the contact's, so a
  call can stop part way; the error then says what was saved. Nothing is retried.
- **Seen at once:** the address book is refreshed right after a write, so `contacts_search`,
  `contacts_get` and `find_person` show the change on the next call.
- **Limits:** one contact per call; name fields and title up to 255 characters, note 20,000,
  other values 1000, labels 100; at most 20 entries per list (create) or in `add` and `remove`
  together (update); at most 10 group names per call. The token's `max_writes_per_hour`
  counts mail, calendar and contact writes together.
- Write calls are audited with `"write": true` and no contact content: no id, name, number,
  address, email, note, label or group name.

Out of scope: creating, renaming or deleting groups, photos, merging duplicates, switching a
card between person and company, and batch changes.

**Contacts** are kept in memory only (never written to disk): the first call (or startup)
loads the address book, and later calls fetch only what changed, at most once a minute (and
right after a contact write).
`find_person` joins them with the local mail index, so it answers in milliseconds. It does
not read calendars; for meetings with someone, use `calendar_search`.

If iCloud throttles an account (HTTP 503 with `Retry-After`), the server stops sending it
calendar and contacts requests until the given time, instead of retrying into the block.

**Subscribed calendars** (webcal, e.g. TripIt, school or team schedules) can be included:
iCloud does not serve their events, so the server fetches each feed from its publisher, as
Apple's apps do, and expands recurring events itself. The example config leaves this off;
turn it on per account with `calendar.subscriptions: true` after reading the security notes
below.

Capped results are paged the same way everywhere: pass `limit` and `offset`, and call again
with `offset` = `next_offset` until it is `null`. Each tool's description states its limits.

Every result carries `"content_trust": "untrusted"` and provenance. Email, event and contact
content can contain prompt-injection attempts; tool descriptions tell the model to treat it as data only.

### How search stays fast

IMAP has no cross-folder search, and searching ~40 folders one by one took 13–20 s against
iCloud. The server therefore keeps a **local SQLite FTS5 index per account** (envelope plus
plain-text body, bounded by `max_age_days`, default 730). A background worker backfills it,
re-syncs changed folders every 5 minutes (skipping unchanged ones via CONDSTORE), and IDLEs on
INBOX for near-instant new mail. `mail_get` and `mail_thread` read live from iCloud.

## Security model

See [SECURITY.md](SECURITY.md) for how to report a vulnerability and the threat model.

- **Bearer tokens** authenticate every `/mcp` request. Config stores only their SHA-256
  hashes; lookup is constant-time. Missing or unknown token → `401`.
- **`/healthz`** is open (for container health checks) but returns only `{"status"}`:
  `200` unless every account's mail sync has failed, then `503`. Called with a valid bearer
  token it adds that token's own account's sync state and index size.
- **Per-account isolation:** separate index file, IMAP connection pool and sync worker per
  account. A token cannot name another account.
- **Folder scope** per account: everything by default, with an `exclude` list (e.g. banking or
  medical folders). `Junk`, `Deleted Messages` and `Drafts` are hidden unless turned on with
  `mail.special_folders`. Excluded folders are purged from the index on the next sync.
- **iCloud credentials** are app-specific passwords from the environment, never from config.
- **Audit log:** one JSON line per tool call on stdout (account, token label, tool, result
  count, latency; `"write": true` for mail writes). Search text, ids and folder names are
  redacted to their length.
- **Container:** non-root (10001:10001), read-only root filesystem, no capabilities.
- **No TLS** in the server itself: run it on a private network (LAN, VPN, Tailscale) or
  behind a TLS reverse proxy. Do not expose it directly to the internet.
- The index holds **mail bodies**: put `/data` on an encrypted disk.
- **Outbound requests** go to iCloud (including `smtp.mail.me.com:587`, only for `mail_send`
  calls by a token granted `send`) and, for subscribed calendars, to each feed's publisher.
  Feed requests carry no iCloud credentials, must resolve to public addresses (a feed URL
  cannot reach your LAN; the connection goes to the address that was checked), are size- and
  time-limited, and feed URLs (which often contain private tokens) are never logged or
  returned. [docs/egress.md](docs/egress.md) shows host firewall rules that limit the
  container to iCloud (or, with subscriptions on, to public hosts).

## Quick start (Docker Compose)

1. Create an **app-specific password** for each Apple ID: appleid.apple.com → Sign-In and
   Security → App-Specific Passwords.
2. Copy the templates. The copies are per deployment and gitignored:

   ```sh
   cp compose.example.yml compose.yml
   cp pv-icloud.env.example pv-icloud.env
   cp config/pv-icloud.example.yaml config/pv-icloud.yaml
   ```

3. Generate a bearer token for each client and put the **hash** in `config/pv-icloud.yaml`:

   ```sh
   docker run --rm ghcr.io/happycynic/pv-icloud:latest hash-token
   ```

   Keep the plaintext token for the client; it is not stored anywhere else.
4. Fill in `pv-icloud.env` (`PV_ICLOUD_<ID>_ICLOUD_USER` / `PV_ICLOUD_<ID>_APP_PASSWORD`,
   where `<ID>` is the upper-cased account id from the config).
5. `docker compose up -d`, then check `curl localhost:8771/healthz`. The first backfill takes
   a while (roughly 5 messages/s); search covers what has been indexed so far.
6. Connect a client, e.g. Claude Code:

   ```sh
   claude mcp add --transport http icloud http://localhost:8771/mcp \
     --header "Authorization: Bearer <token>"
   ```

### Volumes

| Path | Kind | Contents |
|---|---|---|
| `/data` | **persisted** | Per-account mail index (`<account_id>/mail.sqlite3`). Rebuildable from iCloud, but slow. Encrypt at rest. |
| `/config/pv-icloud.yaml` | mounted read-only | Accounts, token hashes, folder scope |
| env file | secrets | Apple IDs and app-specific passwords |
| `/tmp` | tmpfs | Scratch; nothing else is writable |

## Configuration

See [`config/pv-icloud.example.yaml`](config/pv-icloud.example.yaml). Per account:

| Key | Default | Meaning |
|---|---|---|
| `mail.folders.include` / `exclude` | `"*"` / `[]` | Folder scope (display names, globs allowed) |
| `mail.special_folders.junk` / `deleted_messages` / `drafts` | `false` | Show `Junk`, `Deleted Messages`, `Drafts` (hidden by default) |
| `mail.max_age_days` | `730` | Index window; searches are clamped to it |
| `mail.max_connections` | `2` | IMAP connections (iCloud limits per Apple ID); one is reserved for sync |
| `mail.write.protected_folders` | `[]` | No mail write moves messages into or out of these folders; an account's list adds to the defaults' |
| `mail.write.archive_folder` | `Archive` | Where `mail_archive` moves messages |
| `mail.write.send_from` | unset | `From` of drafts and sent mail (one bare address, `local@domain`); required when a token of the account has `send`; unset: drafts have no `From`, and Mail fills in the default sender |
| `calendar.calendars.include` / `exclude` | `"*"` / `[]` | Calendar scope (display names, globs allowed) |
| `calendar.max_range_days` | `90` | Longest `calendar_events` range |
| `calendar.max_search_days` | `730` | Longest `calendar_search` range |
| `calendar.subscriptions` | `true` (example config: `false`) | Fetch subscribed (webcal) feeds from their publishers; `false` lists them without events |
| `calendar.timezone` | from iCloud | Zone for date-only ranges and new events (IANA name, e.g. `Europe/Berlin`) |
| `calendar.write.protected_calendars` | `[]` | Calendar display names (exact) that calendar writes never change; an account's list adds to the defaults' |
| `contacts.enabled` | `true` | Contacts tools for this account; `find_person` then uses mail only |
| `contacts.exclude_groups` | `[]` | Hide every contact in these groups (names, globs allowed) |

Per token (under `accounts[].tokens[]`, besides `label` and `sha256`):

| Key | Default | Meaning |
|---|---|---|
| `mail_write` | `[]` | Mail write actions this token may use: `move`, `archive`, `delete`, `draft`, `send`. Needs `PV_ICLOUD_MAIL_WRITE=enabled`, otherwise startup fails |
| `protected_folders` | `[]` | Added to the account's protected folders for this token only |
| `calendar_write` | `false` | `true`: this token may create, change and delete events. Needs `PV_ICLOUD_CALENDAR_WRITE=enabled`, otherwise startup fails |
| `protected_calendars` | `[]` | Added to the account's protected calendars for this token only |
| `contacts_write` | `[]` | Contact write actions this token may use: `create`, `update`, `delete`. Needs `PV_ICLOUD_CONTACTS_WRITE=enabled` and `contacts.enabled: true`, otherwise startup fails |
| `max_writes_per_hour` | `30` | This token's write limit for mail, calendar and contact writes together (sliding hour, held in memory) |
| `send_to` | unset (anyone) | Only with `send`: the recipients `mail_send` may reach, as bare addresses or `@domain` entries. Unset: anyone, with a startup warning |

Environment: `PV_ICLOUD_CONFIG` (config path), `PV_ICLOUD_DATA_DIR` (index root),
`PV_ICLOUD_PORT` (default 8771), `PV_ICLOUD_HOST` (default `127.0.0.1`; the image sets
`0.0.0.0`; any non-loopback address logs a plain-HTTP warning at startup),
`PV_ICLOUD_LOG_LEVEL`, `PV_ICLOUD_MAIL_WRITE` (`enabled` turns mail writes on; unset, empty
or `disabled` leaves them off; any other value is a config error), `PV_ICLOUD_CALENDAR_WRITE`
and `PV_ICLOUD_CONTACTS_WRITE` (the same for calendar and contact writes).

## Resources and backups

Measured on one account (2026-10-09); expect roughly linear growth with more mail and more
accounts.

| | |
|---|---|
| **Disk** (`/data`) | About 65 MB per 10,000 indexed messages (plain-text bodies + full-text index). Only mail inside `max_age_days` is indexed. |
| **Memory** | About 105 MB resident with one account after mail, calendar and contacts use (1,500 contacts cached in memory). `compose.example.yml` caps the container at 512 MB. |
| **First start** | The initial backfill downloads every message in the window at roughly 5 messages per second; search covers what is indexed so far. Later syncs only fetch changes. |

**Backups:** `/data` is a cache of what is in iCloud, so you do not need to back it up: if it
is lost, the server rebuilds it on the next start (slowly, as above). If your backups include
it anyway, encrypt them, because the index holds mail bodies. The config file and the
environment file are the only things to keep.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/), or use the included dev container.
See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks and ground rules, and
[CHANGELOG.md](CHANGELOG.md) for what changed in each version.

```sh
uv sync
uv run pytest             # IMAP, CalDAV and CardDAV are faked; no iCloud account needed
uv run ruff check . && uv run mypy
uv run pv-icloud          # serve, using PV_ICLOUD_CONFIG and env credentials
```

Design notes and decisions: [`docs/design.md`](docs/design.md) and [`docs/adr/`](docs/adr/).

## License

[Apache License 2.0](LICENSE); see also [NOTICE](NOTICE).
