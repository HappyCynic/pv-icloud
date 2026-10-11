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
