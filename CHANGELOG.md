# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). GitHub Releases link the container image for
each version.

## [Unreleased]

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

[Unreleased]: https://github.com/HappyCynic/pv-icloud/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/HappyCynic/pv-icloud/releases/tag/v0.1.0
