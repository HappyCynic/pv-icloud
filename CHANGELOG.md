# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). GitHub Releases link the container image for
each version.

## [Unreleased]

### Added

- Opt-in calendar writes ([ADR-0005](docs/adr/0005-opt-in-calendar-write.md)), off by
  default:
  - Deployment switch `PV_ICLOUD_CALENDAR_WRITE=enabled`, independent of the mail switch.
    Without it nothing changes: the write code is not loaded, and a token with
    `calendar_write: true` is a config error.
  - Per-token `calendar_write: true` (default `false`). A token without it sees exactly the
    tools and instructions it saw before.
  - `calendar_event_create`: one event, single or recurring (daily, weekly on chosen days,
    monthly or yearly, with `interval` and `count` or `until`), with title, timed or all-day
    start and end, IANA time zone, location, notes and display alerts.
  - `calendar_event_update`: changes the given fields of one event, or of one occurrence of
    a series only; `clear` removes location, notes or alerts.
  - `calendar_event_delete`: deletes one event, or one occurrence of a series.
  - Only in the account's own calendars and calendars shared with it read-write; subscribed,
    read-only shared and protected calendars fail like a missing calendar. Protected
    calendars: `calendar.write.protected_calendars` in the defaults and per account, plus a
    token's own `protected_calendars`.
  - No attendees and no email: the tools take no attendees and refuse events with attendees
    other than the account or organised by someone else, since iCloud emails attendees
    whatever the client asks.
  - Every update and delete re-reads the event and is conditional on it being unchanged; an
    event changed in Calendar meanwhile fails with `the event changed in Calendar; read it
    again`. Nothing is retried.
  - Calendar writes count against the token's `max_writes_per_hour`, together with mail
    writes, and are audited with `"write": true` and no event content (not even times).
- `calendar_series_update` ([ADR-0007](docs/adr/0007-calendar-series-edit.md)), for tokens
  with `calendar_write`: changes a whole recurring series (`replace_all`, `update_all`) or a
  series from a date on (`replace_from`, `update_from`). The `replace_*` modes discard
  deleted and changed occurrences; the `update_*` modes keep them where they fit the new
  pattern, list the rest in `unmatched`, and drop a changed occurrence only with
  `drop_unmatched: true`. `preview` shows the result without changing anything (it counts
  against the write limit). A change from a date splits the series as Apple Calendar does,
  so ids of occurrences from that date change; a series split in Calendar is changed as one,
  up to 10 parts, with field edits only across parts.
- Opt-in contact writes ([ADR-0006](docs/adr/0006-opt-in-contact-write.md)), off by default:
  - Deployment switch `PV_ICLOUD_CONTACTS_WRITE=enabled`, independent of the mail and
    calendar switches. Without it nothing changes: the write code is not loaded, and a token
    with `contacts_write` is a config error.
  - Per-token `contacts_write: [create, update, delete]`, each action granted on its own
    (default: none); a grant on an account with `contacts.enabled: false` is a config error.
    A token without it sees exactly the tools and instructions it saw before.
  - `contacts_create`: one contact with any field `contacts_get` shows, with labels written
    as Apple Contacts writes them, optionally in existing groups.
  - `contacts_update`: changes, adds (`add`) or removes (`remove`) fields of one contact, and
    adds it to or removes it from existing groups. The stored card is edited in place: every
    line the call does not change, the photo included, stays byte for byte; only `REV` and a
    display name derived from the old name change along. Anything ambiguous is refused with
    `cannot change this contact safely`.
  - `contacts_delete`: deletes one contact, permanently. Never the account owner's own card.
  - Groups are never created, renamed or deleted; contacts in excluded groups stay
    invisible to writes (`contact not found`), and excluded groups give `unknown group`.
  - Every update and delete re-reads the cards it changes and is conditional on them being
    unchanged; a contact changed in Contacts meanwhile fails with `the contact changed in
    Contacts; read it again`. Nothing is retried.
  - The address book is refreshed right after a write, so `contacts_search`, `contacts_get`
    and `find_person` show the change at once.
  - Contact writes count against the token's `max_writes_per_hour`, together with mail and
    calendar writes, and are audited with `"write": true` and no contact content.

### Fixed

- Calendar tools now show the account's own address as the organiser's or an attendee's
  `email`. iCloud stores it as the account's principal path with the address in an `EMAIL=`
  parameter, which was shown as `email: null`; `calendar_search` finds it too.
- The log no longer has httpx's `HTTP Request:` lines at the default `INFO` level. They named
  every request URL, including subscribed calendar feed URLs, which often hold a private
  token, and the path of each event or card written. httpx warnings are still logged.

## [0.2.0] - 2026-10-13

### Added

- `CLAUDE.md` with the rules AI agents must follow in this repository, and
  `.github/CODEOWNERS` requiring maintainer review.
- Spec-driven development with [OpenSpec](https://github.com/Fission-AI/OpenSpec) 1.13.2
  (`openspec/`, Claude Code commands and skills in `.claude/`), and an issue workflow for
  agents: `agent:propose` produces a reviewed proposal, `agent:implement` builds it.
- Opt-in mail writes ([ADR-0004](docs/adr/0004-opt-in-mail-write.md)), off by default:
  - Deployment switch `PV_ICLOUD_MAIL_WRITE=enabled`. Without it nothing changes: the write
    code is not loaded, and a token with a write grant is a config error.
  - `mail_move`, `mail_archive` and `mail_delete`, granted per token with
    `mail_write: [move, archive, delete]`. A token sees only the write tools it was granted;
    a token without `mail_write` sees exactly the read-only tools and instructions.
  - One message per call, moved by a guarded copy-then-remove: the original is removed only
    after iCloud confirms the copy. `mail_delete` only moves mail to Deleted Messages, where
    it stays recoverable until iCloud empties that folder.
  - Protected folders (`mail.write.protected_folders` in the defaults and per account, plus a
    token's own `protected_folders`): nothing moves into or out of them.
  - Per-token write limit `max_writes_per_hour` (default 30), held in memory.
  - `mail.write.archive_folder` (default `Archive`).
  - Write calls are audited with `"write": true` and no folder names.
  - `mail_draft`, granted with `mail_write: [draft]`: saves one plain-text draft in Drafts
    (flagged `\Draft` and `\Seen`) for a person to review and send; nothing is ever sent.
    Optional `in_reply_to` threads it as a reply without marking the original read. At most
    50 addresses, a 998-character subject and a 100,000-character body. Audit lines carry no
    addresses, subject or body.
  - `mail.write.send_from` (optional): the `From` address of drafts and sent mail.
  - `mail_send`, granted with `mail_write: [send]` (separate from `draft`; needs
    `mail.write.send_from`): sends one plain-text message through `smtp.mail.me.com:587`,
    with STARTTLS and a verified certificate required before login. Same inputs and limits
    as `mail_draft`, plus at least one recipient. Bcc addresses go only into the SMTP
    envelope. Exactly one copy ends up in Sent Messages: the server appends one (flagged
    `\Seen`) only if iCloud has not filed its own. Nothing is retried; a send that may or may
    not have gone through says so.
  - Per-token `send_to` allowlist of addresses and `@domain` entries: any other recipient is
    refused before anything is read or sent. A token with `send` and no `send_to` gets a
    startup warning naming it.
  - New outbound connection, only for `mail_send`: `smtp.mail.me.com:587`
    (see docs/egress.md).

### Security

- The MCP SDK's log line for a failed tool call now gives only the length of the error
  text, so an error that repeats the caller's input (such as addresses refused by
  `send_to`) never reaches the server log.

## [0.1.0] - 2026-10-10

### Added

- Read-only MCP server (Streamable HTTP) for iCloud Mail, Calendar and Contacts, serving several iCloud
  accounts; each bearer token maps to exactly one account.
- Mail tools: `mail_folders`, `mail_search`, `mail_get`, `mail_thread`. Search runs on a local
  SQLite FTS5 index, kept current with CONDSTORE/QRESYNC sync and IDLE; messages are read
  live without changing read state.
- Calendar tools: `calendar_list`, `calendar_events`, `calendar_search`, `calendar_event`,
  with recurring events expanded and times shown in each event's own time zone.
- Subscribed (webcal) calendars, fetched from their publishers with public-address checks
  (connections are pinned to the checked address), size and time limits, and feed URLs never
  logged. Off in the example config (`calendar.subscriptions`).
- Contacts tools: `contacts_search`, `contacts_get`, `contacts_groups`, read over CardDAV and
  kept in memory only (never written to disk), refreshed incrementally with `sync-collection`.
  Groups can be hidden per account (`contacts.exclude_groups`), or contacts turned off
  (`contacts.enabled`).
- `find_person`: one call to work out who someone is, joining contacts with the mail index
  (message counts, first and last dates, newest messages per person).
- Back-off when iCloud throttles an account (HTTP 503/429 with `Retry-After`): calendar and
  contacts requests for that account pause until the given time, tools say when to try again,
  and contacts keep answering from the copy already loaded.
- Search ignores case and accents in every tool ("cafe" finds "Café", "muller" finds
  "MÜLLER"), including the mail `from` / `to` / `subject` filters. Mail search always uses the
  local index, even for a single folder.
- Paging (`limit` / `offset` / `next_offset`) on every capped result; tool descriptions state
  the configured limits.
- Per-account configuration: folder and calendar include/exclude, index window, search and
  range limits. `Junk`, `Deleted Messages` and `Drafts` are hidden by default and can be
  turned on per account (`mail.special_folders`).
- Audit log line for every tool call, with search text and ids redacted.
- `/healthz` for container health checks: status only without a token; with a valid bearer
  token, that account's sync state.
- Binds to `127.0.0.1` by default outside the container image (`PV_ICLOUD_HOST`), and warns at
  startup when serving plain HTTP on any other address.
- Container image (amd64 and arm64): non-root, read-only root filesystem, health check, SBOM
  and build provenance; Compose template; host firewall guide for outbound traffic
  (`docs/egress.md`).

[Unreleased]: https://github.com/HappyCynic/pv-icloud/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/HappyCynic/pv-icloud/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HappyCynic/pv-icloud/releases/tag/v0.1.0
