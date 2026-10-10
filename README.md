# Private iCloud MCP (`pv-icloud`)

[![CI](https://github.com/HappyCynic/pv-icloud/actions/workflows/image.yml/badge.svg?branch=main)](https://github.com/HappyCynic/pv-icloud/actions/workflows/image.yml)
[![Release](https://img.shields.io/github/v/release/HappyCynic/pv-icloud?sort=semver)](https://github.com/HappyCynic/pv-icloud/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server that lets MCP
clients (Claude Code, Claude Desktop, …) search and read **iCloud Mail**, **Calendar** and
**Contacts**. One server can serve several iCloud accounts; each bearer token maps to exactly
one account.

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

There are **no** tools that send, move, delete or flag mail, change events or change
contacts. The IMAP layer never issues write commands (folders are opened with `EXAMINE` and
messages fetched with `BODY.PEEK`, so read state never changes), and the CalDAV and CardDAV
clients can only send `PROPFIND` and `REPORT`.

**Contacts** are kept in memory only (never written to disk): the first call (or startup)
loads the address book, and later calls fetch only what changed, at most once a minute.
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
  count, latency). Search text and ids are redacted to their length.
- **Container:** non-root (10001:10001), read-only root filesystem, no capabilities.
- **No TLS** in the server itself: run it on a private network (LAN, VPN, Tailscale) or
  behind a TLS reverse proxy. Do not expose it directly to the internet.
- The index holds **mail bodies**: put `/data` on an encrypted disk.
- **Outbound requests** go to iCloud and, for subscribed calendars, to each feed's publisher.
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
| `calendar.calendars.include` / `exclude` | `"*"` / `[]` | Calendar scope (display names, globs allowed) |
| `calendar.max_range_days` | `90` | Longest `calendar_events` range |
| `calendar.max_search_days` | `730` | Longest `calendar_search` range |
| `calendar.subscriptions` | `true` (example config: `false`) | Fetch subscribed (webcal) feeds from their publishers; `false` lists them without events |
| `calendar.timezone` | from iCloud | Zone for date-only ranges (IANA name, e.g. `Europe/Berlin`) |
| `contacts.enabled` | `true` | Contacts tools for this account; `find_person` then uses mail only |
| `contacts.exclude_groups` | `[]` | Hide every contact in these groups (names, globs allowed) |

Environment: `PV_ICLOUD_CONFIG` (config path), `PV_ICLOUD_DATA_DIR` (index root),
`PV_ICLOUD_PORT` (default 8771), `PV_ICLOUD_HOST` (default `127.0.0.1`; the image sets
`0.0.0.0`; any non-loopback address logs a plain-HTTP warning at startup),
`PV_ICLOUD_LOG_LEVEL`.

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
