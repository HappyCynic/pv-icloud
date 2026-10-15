# Design

How Private iCloud MCP (`pv-icloud`) works and why. Decisions with more background are in
[`docs/adr/`](adr/); the security summary is in [`SECURITY.md`](../SECURITY.md).

## Goals

- Let an MCP client search and read one person's **iCloud Mail, Calendar and Contacts**.
- **Read-only by default**: no read path can change iCloud data or read state. Moving,
  archiving and deleting mail exist only when the deployment switches them on and a token is
  granted them ([Mail writes](#mail-writes); ADR-0004), and so do creating, changing and
  deleting calendar events ([Calendar writes](#calendar-writes); ADR-0005) and contacts
  ([Contact writes](#contact-writes); ADR-0006).
- Serve **several iCloud accounts** from one server, each strictly isolated (ADR-0003).
- Fast enough for interactive use: cross-folder mail search well under a second.
- Run on a small always-on host (a home server or NAS), not on a Mac.

Out of scope: Reminders and Notes (iCloud does not expose current Reminders or Notes over a
standard protocol; ADR-0002), and any write action other than the opt-in mail, calendar and
contact writes.

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
| `mail/` | IMAP pool, message parsing, HTML → text, FTS5 index, sync worker, tools; `mail/imap_write.py` is the only code that changes mail (loaded only when writes are on) |
| `ratelimit` | Per-token write limit, mail, calendar and contacts together (in memory) |
| `dav` | Read-only WebDAV base (`PROPFIND`/`REPORT` only), shared throttle |
| `cal/` | CalDAV client, subscribed-feed fetcher, recurrence expansion, event ids, tools; `cal/caldav_write.py` is the only code that changes calendars and `cal/compose.py` builds the events it writes (both loaded only when calendar writes are on) |
| `contacts/` | CardDAV client, vCard parser, contacts cache and tools; `contacts/carddav_write.py` is the only code that changes contacts and `contacts/compose.py` builds and edits the cards it writes (both loaded only when contact writes are on) |
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
| `mail_move` | *Opt-in write.* Moves one message to another in-scope folder |
| `mail_archive` | *Opt-in write.* Moves one message to the archive folder |
| `mail_delete` | *Opt-in write.* Moves one message to Deleted Messages (never a permanent delete) |
| `mail_draft` | *Opt-in write.* Saves one plain-text draft in Drafts, optionally as a reply; never sends |
| `mail_send` | *Opt-in write.* Sends one plain-text message through iCloud SMTP and keeps one copy in Sent Messages; cannot be undone |
| `calendar_event_create` | *Opt-in write.* Creates one event, single or recurring, without attendees |
| `calendar_event_update` | *Opt-in write.* Changes the given fields of one event or one occurrence |
| `calendar_event_delete` | *Opt-in write.* Deletes one event or one occurrence; cannot be undone |
| `calendar_series_update` | *Opt-in write.* Changes a whole recurring series, or a series from a date on (`replace_all`, `replace_from`, `update_all`, `update_from`), with `preview` |
| `contacts_create` | *Opt-in write.* Creates one contact, optionally in existing groups |
| `contacts_update` | *Opt-in write.* Changes, adds or removes fields of one contact in place, and its group membership |
| `contacts_delete` | *Opt-in write.* Deletes one contact (never the me-card); cannot be undone |

The mail write tools exist only with `PV_ICLOUD_MAIL_WRITE=enabled` and are listed only to
tokens granted them; see [Mail writes](#mail-writes). The calendar write tools exist only
with `PV_ICLOUD_CALENDAR_WRITE=enabled` and are listed only to tokens with `calendar_write`;
see [Calendar writes](#calendar-writes). The contact write tools exist only with
`PV_ICLOUD_CONTACTS_WRITE=enabled` and are listed only to tokens granted their action in
`contacts_write`; see [Contact writes](#contact-writes).

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

## Mail writes

Decided in [ADR-0004](adr/0004-opt-in-mail-write.md). Three layers have to agree before mail
changes:

1. **Deployment switch.** `PV_ICLOUD_MAIL_WRITE=enabled`. Without it `mail/imap_write.py` and
   `mail/smtp.py` are never imported, no write tool is registered, and a token with
   `mail_write` is a config error. Any value other than `enabled`, `disabled` or empty is a
   config error too.
2. **Per-token grant.** `mail_write: [move, archive, delete, draft, send]`, each action on its own. The
   server lists each token only its granted write tools (SDK middleware filters `tools/list`)
   and refuses the others exactly as it refuses a tool that does not exist. A token with
   write actions gets server instructions naming them; a token without gets the read-only
   instructions unchanged (a snapshot test pins its whole `tools/list` and instructions).
3. **Per-call checks**, before anything that can change mail: the token's hourly write limit
   (sliding window, in memory, every granted call counts), the source and destination scope,
   and protected folders (defaults ∪ account ∪ token, so a lower layer can only tighten).

**Guarded move.** iCloud advertises `UIDPLUS` and `UNSELECT` but not `MOVE`, so move,
archive and delete all copy and then remove, on a fresh connection that is logged out
afterwards and never returned to the read pool:

1. No `UIDPLUS` (needed for `COPYUID` and `UID EXPUNGE`): fail before anything is sent.
2. `LIST` to resolve the destination: `mail_move`'s folder must be in scope; Archive is found
   by name (`mail.write.archive_folder`, default `Archive`); Deleted Messages is the `\Trash`
   folder, else the one with that name. Archive and Deleted Messages work even when hidden
   from reads.
3. `SELECT` the source; its `UIDVALIDITY` must match the id; `UID FETCH <uid> (UID FLAGS)`
   confirms the message is there (flags only: after `SELECT` a body fetch would set `\Seen`).
4. `UID COPY <uid>`. The `COPYUID` response must name exactly this UID and the destination's
   `UIDVALIDITY`; otherwise stop, with the original untouched.
5. `UID STORE <uid> +FLAGS.SILENT (\Deleted)`, then `UID EXPUNGE <uid>`, which removes only
   this UID, never other messages someone else marked `\Deleted`. `CLOSE` is never used.

A message leaves its source only after its copy is confirmed. A failure after the copy
leaves a duplicate (or a source copy marked `\Deleted`), never a loss, and the tool error
says which. The copy keeps every flag, and `new_id` comes from `COPYUID`. Afterwards the
old id is dropped from the index and the sync worker resyncs both folders at once.

**Drafts.** `mail_draft` creates one new message and changes no existing one. Its input is
checked first (at most 50 addresses across `to`, `cc` and `bcc`, each exactly one ASCII
address; subject at most 998 characters with no line breaks; body at most 100,000
characters; not entirely empty), then:

1. With `in_reply_to`, the original is read like `mail_get` (`EXAMINE`, `BODY.PEEK[]`, same
   scope and staleness checks), so its `\Seen` flag never changes. The draft gets
   `In-Reply-To` (the original's `Message-ID`), `References` (the original's `References`,
   else its `In-Reply-To`, then its `Message-ID`) and, without a caller subject, `Re: ` + the
   original's subject unless it already starts with `Re:`.
2. `mail/compose.py` builds a plain-text UTF-8 message: `From` from `mail.write.send_from`
   when set, `To`, `Cc`, `Bcc`, `Subject`, `Date`, and a `Message-ID` on the fixed domain
   `pv-icloud.invalid` (never this host's name).
3. On a fresh write connection: no `UIDPLUS` (needed for `APPENDUID`) fails before anything
   is sent; `LIST` finds the folder named `Drafts` (iCloud gives it no special-use
   attribute), even when hidden from reads; `APPEND "Drafts" (\Draft \Seen)` saves it. No
   `SELECT`. The new id comes from `APPENDUID`; if iCloud saved the draft without one, the
   call still succeeds with `id: null`, so a caller has no reason to retry into a duplicate.
4. If Drafts is in the read scope, the sync worker resyncs it.

`mail_draft` sends nothing and opens no SMTP connection: a person sends the draft from Mail.
Protected folders fence moves only; the `draft` grant is what controls drafting.

**Sending.** `mail_send` (grant `send`, separate from `draft`) is the only code path that
sends mail, and a sent message cannot be recalled, so every refusal happens before the SMTP
connection:

1. The draft input limits, plus at least one recipient.
2. The token's `send_to`, if it has one: every `to`, `cc` and `bcc` address, lower-cased,
   must equal an address entry or have exactly the domain of an `@domain` entry
   (`@example.org` does not match `a@mail.example.org`). Otherwise the call fails naming
   only the refused addresses, before anything is read. Replying to someone does not add
   them. A token with `send` and no `send_to` may send anywhere and is named in a startup
   warning.
3. With `in_reply_to`, the original is read like `mail_get`, as for drafts.
4. `LIST` on a read connection finds Sent Messages (the `\Sent` folder, else the one with
   that name), even when hidden from reads; without one the call fails here.
5. `mail/compose.py` builds the message: `From` is `mail.write.send_from` (required for
   `send`), the `Message-ID` is on that address's domain, and the body is 7-bit clean
   (quoted-printable or base64), so it needs no `8BITMIME`. The bytes sent have no `Bcc`
   header; the Bcc addresses go only into the SMTP envelope.
6. `mail/smtp.py`, the only module that imports `smtplib`, connects to
   `smtp.mail.me.com:587` with a 30-second timeout. It requires `STARTTLS` with
   `ssl.create_default_context()` (certificate and host name verified, and the module
   refuses any context that does not verify) before `AUTH`; a server that does not offer
   it, or a failed handshake, gets nothing more. Then `MAIL FROM`, `RCPT TO` for each
   recipient, `DATA`. All recipients refused means nothing was sent; some refused is a
   success that lists them in `refused_recipients`. A connection lost during `DATA` is
   reported as "may have been sent; check Sent Messages", and nothing is retried. Errors
   and logs never carry the server's reply text, which can echo addresses.
7. Exactly one copy in Sent Messages: on a read connection, `EXAMINE` and
   `UID SEARCH HEADER Message-ID "<id>"`. If iCloud filed its own copy, nothing is
   appended; otherwise (or if that check fails) one `APPEND <Sent Messages> (\Seen)` of the
   message with its `Bcc` header, as mail clients keep it. If that append fails the call
   still succeeds (the mail has gone) with `sent_copy_saved: false`. If Sent Messages is in
   the read scope, the sync worker resyncs it.

Protected folders do not apply to sending or to the sent copy; the `send` grant (and
`send_to`) is the control. The MCP SDK logs each tool error's text at INFO; a logging filter
reduces it to its length, so refused addresses never reach the log.

`tests/test_repo_policy.py` inspects the code's IMAP call sites: only `mail/imap_write.py`
may `SELECT`, and only with these single-UID forms, and `APPEND` only with exactly
`(\Draft \Seen)` or `(\Seen)`; nothing may `STORE`, `COPY`, `EXPUNGE`, `CLOSE`, `MOVE` or
touch folders. It also checks that only `mail/smtp.py` imports `smtplib`. The fake IMAP
server rejects every other form at run time (an `APPEND` only into Drafts or Sent Messages,
each with its own flags), and the fake SMTP server fails a test that sends `AUTH`, `MAIL` or
`DATA` before `STARTTLS`.

## Calendar

A small CalDAV client on `httpx` (only `PROPFIND` and `REPORT` can be sent; the opt-in
[calendar writes](#calendar-writes) have their own module). Discovery goes
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

## Calendar writes

Creating, changing and deleting events (ADR-0005) follows the pattern of mail writes: a
deployment switch (`PV_ICLOUD_CALENDAR_WRITE=enabled`; without it `cal/caldav_write.py` and
`cal/compose.py` are never imported and no calendar write tool is registered), a per-token
grant (`calendar_write: true`; a grant without the switch is a config error), a per-token
tools list and instructions, protected calendars, the token's hourly write limit (shared with
mail writes) and audit lines with `write: true` and no content. Every argument of a calendar
write is redacted in the audit line, times included.

**Writable calendars.** The calendar list's `PROPFIND` also asks for each calendar's
`current-user-privilege-set`. A write reaches only an in-scope calendar of kind `calendar`
whose privileges include `write-content` (directly or through `write` or `all`): the
account's own calendars and calendars shared with it read-write. Subscribed calendars,
calendars shared read-only, calendars in `calendar.write.protected_calendars` (defaults plus
account) or in the token's `protected_calendars` (exact display names) all fail with the same
`unknown calendar` error as a missing or excluded one, before anything is sent, so iCloud's
403 is never relied on. An event id's resource path must be a direct `.ics` child of that
calendar.

**Fresh reads and conditional writes.** `cal/caldav_write.py` is the only module that sends
`PUT` or `DELETE`, always for one event resource, always with `Schedule-Reply: F`, and always
conditional: a new event (a random UUID as both `UID` and resource name) is a `PUT` with
`If-None-Match: *`; an update or delete first re-reads that one resource with a single-href
`calendar-multiget` (unexpanded; iCloud answers a Depth 0 `calendar-query` on a resource URL
with 403) and sends `If-Match` with exactly the ETag it just read. A 412 means the event
changed in Calendar, or an attendee's reply changed it on the server, since that read: the
call fails with `the event changed in Calendar; read it again`, and the other change stays.
Nothing is retried, not even after a timeout (which is reported as unconfirmed); 429 and 503
pause the account's CalDAV and CardDAV requests like a read would. `dav.py` is unchanged and
still refuses anything but `PROPFIND` and `REPORT`, so CardDAV and every read keep their
guarantee.

**Occurrences.** The id names an occurrence by its start, as the read tools write it. The
stored resource is searched for an override with that start, then a single event with it,
then the series is expanded locally (`cal/expand.py`) to find it. Changing one occurrence
edits its override, or adds one (a copy of the master without `RRULE`, `RDATE` and `EXDATE`,
with `RECURRENCE-ID` the original start in the master's value type and zone); deleting one
adds an `EXDATE` to the master and drops its override. A single event is deleted with
`DELETE`. These three tools never change or delete a whole series; only
`calendar_series_update` changes one (see [Series edits](#series-edits)). Every other component and
property of the resource (other overrides, `VTIMEZONE`s, `X-APPLE-*` properties, alerts not
being replaced) is kept; the changed component gets a higher `SEQUENCE` and new
`LAST-MODIFIED`. New and moved times are written with `TZID` and a matching `VTIMEZONE`, never
as floating times; alerts are written only as display alerts.

**Why attendees are refused.** iCloud schedules on the server and ignores
`SCHEDULE-AGENT=CLIENT` (live check, 2026-10-13): any event saved with an attendee makes it
send invitations, updates or cancellations. So pv-icloud never writes `ORGANIZER` or
`ATTENDEE`, and refuses, after the fresh read and before any write, a resource any of whose
events has an organiser or attendee other than the account itself. iCloud stores the
account's own address as its principal path with the address in `EMAIL=`; such a value, a
`mailto:` address from the principal's `calendar-user-address-set` (read once per account),
or the principal path itself count as the account. Anything else, a principal path without a
known `EMAIL=` included, counts as someone else. The read tools take `email` from `EMAIL=` in
the same way. The fake CalDAV server records every write that would make iCloud email
someone, and the tests require that record to stay empty.

### Series edits

`calendar_series_update` ([ADR-0007](adr/0007-calendar-series-edit.md)) changes a whole
recurring series, or a series from a date on, under the same switch, grant, fences and write
limit (one call, a preview included, counts once). Its four modes:

| Mode | Occurrences before `effective` | Deleted and changed occurrences in range |
|---|---|---|
| `replace_all` | (no date) | discarded |
| `replace_from` | as stored | discarded |
| `update_all` | (no date) | kept where they fit the new pattern |
| `update_from` | as stored | kept where they fit the new pattern |

- **The new definition.** The fields of `calendar_event_update` plus `recurrence` (as for
  `calendar_event_create`; it replaces the rule). Fields not given keep the series' values;
  `start` and `end` are the times of the first changed occurrence. The new `DTSTART` is the
  first occurrence the new rule generates from there, so no occurrence outside the rule is
  added (RFC 5545 always counts `DTSTART`). `start`, `end`, `all_day`, `timezone` and
  `recurrence` are *pattern* fields; title, location, notes and alerts are *field edits*.
- **Matching exceptions** (`update_*`). An `EXDATE` or override is kept when the new series
  has exactly one occurrence on the day of its original start (in the series' zone); it is
  re-keyed to that occurrence. A kept override takes a changed field only where it still had
  the series' value, as Apple's apps do, and its times only if it was not moved. Changing
  `all_day` keeps no override. Exceptions that no longer fit are listed in `unmatched`: a
  deleted one is dropped; a changed one fails the call unless `drop_unmatched: true`.
- **From a date, as Apple Calendar does.** The stored series is *cut*: its rule gets
  `UNTIL` = 23:59:59 local time on the day before `effective`, written in UTC (a `DATE` for
  an all-day series), replacing a `COUNT`, and loses its exceptions from the date on;
  everything earlier is kept as stored. The rest becomes a new series (new `UID`, a copy of
  the master's properties and alarms, no `ORGANIZER` or `ATTENDEE`) in a new resource. Both
  carry one `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value, as does every
  override pv-icloud creates or re-keys. Ids of occurrences from the date change; the result
  says so (`ids_changed_from`). When `effective` is on or before the series' first
  occurrence, nothing is cut and the series is changed in place (`parts.split: false`).
- **Order of writes.** Everything is read, checked and worked out first (a preview stops
  there). Then the new resource (`If-None-Match: *`), then the cut (`If-Match`). If the cut
  is refused (412, 404 or another 4xx), the new resource is read once and deleted with
  `If-Match`, so the calendar is as it was. If the cut gets no answer or is throttled, or the
  rollback fails, nothing more is sent and the error says the series may be stored twice,
  naming the new series' id. Nothing is retried.
- **Series split in Calendar.** Apple treats every resource sharing a recurrence-set value
  (a *part*) as one series, and so does this tool, for up to 10 parts in the series'
  calendar. iCloud does not support property filters in `calendar-query` (a `prop-filter`
  matches nothing, a `collation` gives 412, a `param-filter` 500), so parts are found with
  read-only, unexpanded time-range listings of that calendar, matched locally: from the
  named part's last occurrence (or from `effective`) with no end, and, in the `_all` modes
  only, from 366 days before today to the named part's first occurrence. Older parts are not
  found or changed (`parts.searched_from`). Across parts only field edits are applied, one
  `PUT` with `If-Match` per part; a pattern change or `replace_*` that would reach more than
  one part is refused. A failure after the first write stops the call and the error names the
  parts already changed (only a summary is logged); nothing is undone except a split's own
  new resource.

`tests/test_repo_policy.py` checks that `PUT` and `DELETE` are used as HTTP methods only in
`cal/caldav_write.py`, that no source sends `MKCALENDAR`, `MKCOL`, `PROPPATCH`, `MOVE`,
`COPY`, `ACL`, `LOCK` or `POST`, and that nothing under `cal/` writes `ORGANIZER` or
`ATTENDEE`. The fake server refuses unconditional writes and writes to collections,
subscribed or read-only calendars.

## Contacts

A CardDAV client on the same read-only base. Each account's address book is cached **in
memory only** (contacts never reach `/data`): loaded once in the background at startup, then
kept current with `sync-collection`, at most once a minute, fetching only changed cards, and
right after every contact write.

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

## Contact writes

Creating, changing and deleting contacts (ADR-0006) follows the pattern of calendar writes:
a deployment switch (`PV_ICLOUD_CONTACTS_WRITE=enabled`; without it
`contacts/carddav_write.py` and `contacts/compose.py` are never imported and no contact write
tool is registered), a per-token grant per action (`contacts_write: [create, update,
delete]`; delete is its own grant because it is permanent; a grant without the switch, or on
an account with `contacts.enabled: false`, is a config error), a per-token tools list and
instructions, the token's hourly write limit (shared with mail and calendar writes) and audit
lines with `write: true` in which every argument, the id included, is redacted.

**Lossless edits.** The reader drops what it does not show (photos, `X-APPLE-…` and unknown
properties), so a card is never rebuilt from the parsed contact. `contacts/compose.py` splits
the stored text into logical lines that keep their exact bytes (folds and line endings),
rewrites only the lines a change names, adds new lines just before `END:VCARD`, and emits
every other line as stored, in order. Structured properties (`N`, `ORG`) keep their
parameters and the components not changed, escaped as stored. Two properties change without
being named, as Apple's apps do: `REV` is set to the current UTC time on every card a call
writes (added if missing), and `FN` follows a name change only if it equalled the old name
parts (for a card shown as a company, the old organization), so a custom display name is
kept. A removed value takes its item group's label lines with it. A change is refused, with
nothing written, when a single-valued property it changes appears twice, a card has two
`REV` lines, a value to remove shares its item group with another property or matches more
than one value, the card is not vCard 3.0, or a line cannot be split. New lines use the
card's own line ending; card text is read with each CR as `&#13;` (as Apple's server sends
it), since an XML parser turns a literal CR LF into LF.

**Labels** are written in the forms Apple's apps write (checked on a live address book,
2026-10-14): one `TYPE=` parameter per type, `IPHONE` before `CELL`, `VOICE` only on the
built-in voice types (`mobile`, `iPhone`, `home`, `work`), `INTERNET` first on emails and
`PREF` last; other labels go in an item group with `X-ABLabel` (Apple's built-in ones as
`_$!<Other>!$_`). Unlabelled and item-group phones carry no `VOICE`, and item-group emails no
`HOME` or `WORK`. Tests pin both the bytes and the label `contacts_get` reads back.

**Groups.** Membership lives in the group's card (`X-ADDRESSBOOKSERVER-MEMBER`), so adding a
contact to a group or removing it is a lossless edit of that card: one member line added or
removed, `REV` bumped, every other line (members hidden from the caller included) kept.
Groups are never created, renamed or deleted. A delete leaves the deleted contact's member
entries in group cards, as Apple's own clients do; the reader ignores them.

**Visibility.** A write reaches only a contact `contacts_get` would return: an id is looked
up in the in-memory snapshot (never used to build a URL), and members of an excluded group,
group cards and unknown ids all fail with the same `contact not found`. A group named in a
call must be visible; an excluded one gives `unknown group`, like a missing one. The me-card
can be updated but not deleted.

**Fresh reads and conditional writes.** `contacts/carddav_write.py` is the only module that
sends `PUT` or `DELETE`, always for one `.vcf` directly inside the account's address book
(itself directly under the discovered home), always conditional: a new card (a random UUID as
file name and `UID`) with `If-None-Match: *`; an update or delete only after one
`addressbook-multiget` re-reads the contact card and every group card the call changes, with
`If-Match` and exactly the ETags just read. Everything is checked against that read before
the first write. The contact card is written first, then each group card in name order; if
one fails (a 412 because it changed in Contacts, throttling, a network error), the call stops
and its error says what was saved. Nothing is rolled back or retried. After any write the
address book is refreshed at once; if that refresh fails, what was written is applied to the
copy in memory, which is marked stale.

`tests/test_repo_policy.py` checks that `PUT` and `DELETE` are used as HTTP methods only in
the two write modules, that no source sends another DAV write method, that nothing under
`contacts/` writes a `PHOTO` line, and that the two write modules do not import each other.
The fake CardDAV server refuses unconditional writes, writes outside the address book and
deletes of group cards or the me-card, and records each attempt; the tests require that
record to stay empty.

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
label, tool, redacted arguments, result count and latency; search text, ids and folder names
are reduced to their length. Write calls also carry `write: true`, and a call refused by
the write limit has outcome `throttled`. httpx's own request log (which names every URL, so
card and event paths and subscribed feed URLs) is kept to warnings.

`/healthz` is open for container health checks and returns only `{"status"}` (503 only when
every account's mail sync has failed). With a valid bearer token it adds the caller's own
account's sync state.

## Limits and edge cases

Behaviour that is easy to miss when reading results:

- **Date ranges.** A date-only `start` means that day's midnight and a date-only `end` means
  the end of that day (inclusive); datetimes are exclusive at the end. Values without a zone
  use the account's default zone (`calendar.timezone`, else the zone of its iCloud
  calendars).
- **Recurrence in subscribed feeds.** Sub-hourly rules (`SECONDLY`, `MINUTELY`) yield only
  their first occurrence, and `RANGE=THISANDFUTURE` overrides are applied to a single
  occurrence. iCloud calendars are unaffected: iCloud expands those itself.
- **Bounds.** Thread discovery stops at 1,000 messages; `find_person` examines the newest
  2,000 matching messages per call, so its counts are lower bounds for very busy addresses.
- **Paging.** Offsets index a fresh result set on every call, so mail that arrives between
  two pages can shift results by one.
- **Folder counts** in `mail_folders` come from the index and can be up to one sync pass
  (5 minutes) old; `count_source` and `counts_as_of` say so. Before the first sync they are
  read live.
- **Throttling.** `Retry-After` is honoured as seconds or as an HTTP date; without one the
  pause is 60 s, and no pause is longer than an hour. The account's DAV home URL is
  rediscovered only after a client error (4xx other than 429), never after throttling,
  server errors or network failures, which rediscovery would not fix.
- **No deep links.** iCloud Mail has no per-message URLs; `web_url` opens the mailbox, and
  the result says so.

## Deployment

A two-stage image on `python:3.14-slim`, built for amd64 and arm64, runs as uid 10001 with a
read-only root filesystem; `/data` is the only writable path. CI runs ruff, mypy and the tests
(against in-process fakes of iCloud's IMAP, CalDAV and CardDAV behaviour), then publishes the
image with an SBOM and build provenance. Tagging `vX.Y.Z` creates a GitHub Release with the
image digest. See the README for Compose setup and [`egress.md`](egress.md) for restricting
outbound traffic.
