# Design

How Private iCloud MCP (`pv-icloud`) works and why. Decisions with more background are in
[`docs/adr/`](adr/); the security summary is in [`SECURITY.md`](../SECURITY.md).

## Goals

- Let an MCP client search and read one person's **iCloud Mail, Calendar and Contacts**.
- **Read-only by construction**: no code path can change iCloud data or read state.
- Serve **several iCloud accounts** from one server, each strictly isolated (ADR-0003).
- Fast enough for interactive use: cross-folder mail search well under a second.
- Run on a small always-on host (a home server or NAS), not on a Mac.

Out of scope: Reminders and Notes (iCloud does not expose current Reminders or Notes over a
standard protocol; ADR-0002), and any write action.

## Overview

```
MCP client ──HTTP /mcp (bearer)──▶ pv-icloud ──IMAP──────▶ imap.mail.me.com
                                     │        ──CalDAV────▶ caldav.icloud.com (pNN-…)
                                     │        ──CardDAV───▶ contacts.icloud.com (pNN-…)
                                     │        ──HTTPS─────▶ subscribed-calendar publishers (opt-in)
                                     ▼
                         /data/<account>/mail.sqlite3 (FTS5 mail index)
```

One process: the MCP server (official Python SDK, stateless Streamable HTTP with JSON
responses at `/mcp`), plus per account a mail sync thread, an IMAP connection pool, a CalDAV
client and a CardDAV client with an in-memory contacts cache.

| Module | Role |
|---|---|
| `config` | YAML config, per-account defaults and overrides |
| `auth` | Bearer token → account (SHA-256, constant-time) |
| `server` | Tool definitions, audit log, `/healthz`, per-account runtime |
| `mail/` | IMAP pool, message parsing, HTML → text, FTS5 index, sync worker, tools |
| `dav` | Read-only WebDAV base (`PROPFIND`/`REPORT` only), shared throttle |
| `cal/` | CalDAV client, subscribed-feed fetcher, recurrence expansion, event ids, tools |
| `contacts/` | CardDAV client, vCard parser, contacts cache and tools |
| `people` | `find_person`: contacts joined with the mail index |

## Tools

| Tool | What it returns |
|---|---|
| `mail_folders` | Folders and message counts |
| `mail_search` | Messages matching full text and/or from, to, subject, folders, dates |
| `mail_get` | One message: headers, plain-text body (paged by characters), attachment list |
| `mail_thread` | All messages in a thread across folders, oldest first |
| `calendar_list` | Calendars, including subscribed ones, with time zones |
| `calendar_events` | Events in a date range (default: next 7 days) |
| `calendar_search` | Events matching text across all calendars over a long range |
| `calendar_event` | One occurrence with every attendee and the full notes |
| `contacts_search` | Contacts matching names, organisation, emails, phones (any format), notes |
| `contacts_get` | Every field of one contact |
| `contacts_groups` | Groups and member counts |
| `find_person` | Who someone is: their contact card merged with their mail history |

Conventions shared by all tools:

- **Paging.** Every capped result takes `limit` and `offset` and returns `next_offset`
  (`null` on the last page). Long bodies and notes are paged by characters. Each tool
  description states its limits, including per-account limits read from the config, and how to
  page; a test enforces this.
- **Untrusted content.** Every result carries `content_trust: "untrusted"` and provenance
  (`source`, `fetched_at`); tool descriptions tell the model that content may contain
  instructions it must not follow.
- **Opaque ids.** Mail ids encode `folder|UIDVALIDITY|UID`; event ids encode
  `calendar|start|resource`; contact ids are card file names. Ids are only ever resolved
  inside the caller's own account and checked against its scope, so an edited id cannot
  reach another account or an excluded folder or calendar.

## Mail

**Why a local index.** IMAP searches one folder at a time and iCloud has no cross-folder
search (`MULTISEARCH`). Measured against iCloud, a 30-day text search over 39 folders took
13–20 seconds. The server therefore keeps a SQLite FTS5 index per account with the envelope
and plain-text body of every message inside `max_age_days` (default 730). `mail_search`
queries the index, even for a single folder; `mail_get` and `mail_thread` read live so
content is current.

**Matching ignores case and accents** in every tool: "cafe" finds "Café", "muller" finds
"MÜLLER". Full-text queries use FTS5's `unicode61` tokenizer with diacritics removed; the
`from`, `to` and `subject` filters compare folded text (case-folded, accents dropped) through
a SQL function. iCloud's own IMAP `SEARCH` is accent-sensitive, which is one more reason not
to use it.

**Sync.** Folders are only ever opened with `EXAMINE` and messages fetched with `BODY.PEEK`,
so read state and flags never change.

1. After login, `ENABLE CONDSTORE` (iCloud reports `HIGHESTMODSEQ` only once it is enabled).
2. Every 5 minutes: `LIST`; drop index rows for folders that left the scope; `EXAMINE` each
   folder and skip it when `UIDVALIDITY`, `HIGHESTMODSEQ`, `UIDNEXT` and `EXISTS` are
   unchanged. Most passes touch no message.
3. For a changed folder: one `UID SEARCH SINCE <window start>`; UIDs only in the index are
   removed (expunged, moved, aged out), UIDs only on the server are fetched newest first (the
   first 256 KB of each message) and indexed. A new `UIDVALIDITY` re-indexes the folder.
4. Between passes, `IDLE` on INBOX; new or expunged mail there syncs immediately.

Unparseable messages are indexed as stubs so they are not retried every pass; a message
without a usable `Date` is dated by its server receipt time. HTML bodies are converted to text
with a standard-library parser that drops images, scripts and hidden text and never fetches
anything.

**Scope.** Every folder by default, minus a per-account `exclude` list. `Junk`,
`Deleted Messages` and `Drafts` are hidden unless turned on. Leaving the scope purges a
folder's rows on the next sync.

## Calendar

A small CalDAV client on `httpx` (only `PROPFIND` and `REPORT` can be sent). Discovery goes
from `caldav.icloud.com` to the account's `pNN-caldav.icloud.com` home.

- **Events** come from one `calendar-query` per calendar with `<C:expand>`: iCloud expands
  recurring events itself and keeps each event's time zone. Times are returned in the event's
  own zone; all-day events use dates with an inclusive last day; floating times are flagged.
- **A single occurrence** (`calendar_event`) is re-read with a narrow expanded query around its
  start. `calendar-multiget` is not used, because iCloud ignores `<C:expand>` there and returns
  the unexpanded series.
- **Search** reads every in-scope calendar over a long range (default two years centred on
  today) in parallel and matches title, location, notes, calendar name and people.
- **Subscribed (webcal) calendars** are listed by iCloud but served without events, so, when
  `calendar.subscriptions` is on for an account, the server fetches each feed from its
  publisher as Apple's apps do:
  - Only http(s). Each hop, redirects included, must resolve to public addresses only, and
    the connection goes to the address that was checked, with the name in the Host header and
    TLS SNI. A feed URL therefore cannot reach the local network, even through DNS rebinding.
  - No iCloud credentials are sent; 20 MB and 20 s limits; https never downgrades to http.
  - Feed URLs often contain private tokens, so they are never logged or returned; only the
    host name is shown.
  - Feeds are cached in memory for 15 minutes and revalidated with ETag/Last-Modified; if a
    publisher fails, the last good copy is served.
  - Recurrence is expanded locally (on `python-dateutil`): rules are evaluated in the event's
    wall-clock time so they stay correct across DST; `COUNT`, `UNTIL`, `RDATE`, `EXDATE` and
    moved or cancelled occurrences are handled. A malformed rule skips one series, not the
    feed. A common publisher error (`P12H` for `PT12H`) is repaired before parsing.

One failing calendar is reported in the result's `errors` while the rest are returned.

## Contacts

A CardDAV client on the same read-only base. Each account's address book is cached **in
memory only** (contacts never reach `/data`): loaded once in the background at startup, then
kept current with `sync-collection`, at most once a minute, fetching only changed cards.

The vCard parser handles Apple's conventions: item groups with `X-ABLabel` labels, folded lines
(also inside parameters), `TYPE` lists, birthdays without a year, related names, dates and
social profiles. Photos are dropped. Groups can be excluded per account; their members are
then hidden from every tool.

**`find_person`** joins contacts with the mail index: contacts matching the query plus messages
whose From/To/Cc match the query or a matched contact's addresses. Addresses on one card merge
into one person; other addresses become mail-only people. Each person gets message counts,
first and last dates and the newest message ids; the caller's own addresses are marked
`is_me`. It does not read calendars (a long-range calendar scan costs seconds); its description
points the model to `calendar_search` instead.

## iCloud throttling

After many full reloads in a short time, iCloud may answer every CalDAV and CardDAV request
for an Apple ID with HTTP 503 and a `Retry-After` of up to half an hour. The server keeps one
throttle per account: after a 503 or 429 it stops contacting iCloud for that account until
`Retry-After` passes, and tools say when to try again. Contacts keep answering from the cached
copy meanwhile.

## Accounts, auth and logging

See ADR-0003. In short: config holds SHA-256 hashes of bearer tokens; each token belongs to
one account; credentials come from `PV_ICLOUD_<ID>_ICLOUD_USER` and
`PV_ICLOUD_<ID>_APP_PASSWORD`. Each account has its own index file, connection pool, sync
thread, DAV clients and contacts cache. Audit lines (JSON on stdout) record account, token
label, tool, redacted arguments, result count and latency; search text and ids are reduced to
their length.

`/healthz` is open for container health checks and returns only `{"status"}` (503 only when
every account's mail sync has failed). With a valid bearer token it adds the caller's own
account's sync state.

## Deployment

A two-stage image on `python:3.14-slim`, built for amd64 and arm64, runs as uid 10001 with a
read-only root filesystem; `/data` is the only writable path. CI runs ruff, mypy and the tests
(against in-process fakes of iCloud's IMAP, CalDAV and CardDAV behaviour), then publishes the
image with an SBOM and build provenance. Tagging `vX.Y.Z` creates a GitHub Release with the
image digest. See the README for Compose setup and [`egress.md`](egress.md) for restricting
outbound traffic.
