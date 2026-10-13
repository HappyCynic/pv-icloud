# Proposal

## Why

Implements #24. Account owners can read their iCloud calendars through pv-icloud but cannot
change them, so any scheduling change worked out with an agent has to be redone by hand in
Apple's apps. As with mail writes (ADR-0004), different clients deserve different trust, so
calendar writes have to be granted per bearer token and be off unless an operator turns them
on.

## What Changes

- **This change ends the read-only guarantee for Calendar.** CalDAV gains two write methods,
  `PUT` and `DELETE`, sent only from a new write module, `cal/caldav_write.py`, and only for
  a granted calendar write tool call. The DAV base (`dav.py`) keeps refusing every method but
  `PROPFIND` and `REPORT`, so CardDAV and every calendar read stay exactly as they are.
  Contacts stay read-only. No new outbound host is added. Today CLAUDE.md forbids any DAV
  method besides `PROPFIND` and `REPORT`; this proposal and its `design.md` are the design
  discussion, and the rule change itself is a maintainer prerequisite (below).
- **Deployment switch:** `PV_ICLOUD_CALENDAR_WRITE=enabled` (default off), parsed like
  `PV_ICLOUD_MAIL_WRITE`. Without it the write module is never imported, no calendar write
  tool is registered, and a token granted calendar write is a config error.
- **One per-token switch:** `calendar_write: true` (default `false`). A token without it sees
  exactly today's tools, annotations and instructions.
- Three new tools, all listed only to tokens with `calendar_write`:
  - `calendar_event_create`: one single or recurring event with title, start and end (timed
    or all-day dates), time zone, location, notes, attendees and alerts.
  - `calendar_event_update`: change any of those fields on one event; for an occurrence of a
    recurring series, on that occurrence only (the others are unchanged).
  - `calendar_event_delete`: delete one event, or one occurrence of a series.
- **Never sends invitations or replies.** Attendees are written with `SCHEDULE-AGENT=CLIENT`,
  so iCloud is told not to send invitations, updates or cancellations. Events organised by
  someone else, and events whose attendees iCloud would notify, are refused for update and
  delete. Whether iCloud honours `SCHEDULE-AGENT=CLIENT` must be checked on a live account
  before implementation (open question 1).
- **Safe concurrent edits.** Create uses `If-None-Match: *`; update and delete use
  `If-Match` with the ETag just read, so a change made meanwhile in Apple's apps is never
  overwritten: the call fails and asks the caller to read the event again.
- **Scope and isolation:** writes reach only in-scope, non-subscribed event calendars of the
  caller's own account. Excluded calendars and other accounts' calendars fail with the same
  `unknown calendar` error as missing ones; subscribed (webcal) calendars stay read-only.
- **Limits:** one event per call; the token's existing hourly write limit
  (`max_writes_per_hour`, one budget shared with mail writes); field length caps; iCloud's
  throttling back-off applies to writes, and a throttled write is never retried.
- Every write is audit-logged with `write: true` and no event content: titles, notes,
  locations, attendees, times and calendar names are all redacted.
- Docs: README.md, docs/design.md, CONTRIBUTING.md and `config/pv-icloud.example.yaml` are
  updated with the implementation. The rule changes (CLAUDE.md's DAV hard rule,
  `.github/CODEOWNERS`, `openspec/config.yaml`, SECURITY.md and a new ADR-0005) are made by
  the maintainer in a separate pull request **before** implementation starts, as was done for
  mail (#16); they are not tasks of this change.

Out of scope (from #24): subscribed (webcal) calendars, which stay read-only; creating or
deleting calendars; whole-series editing modes such as "this and following" or changing a
series' recurrence rule (the series edit modes issue); sending invitations or responses to
attendees. Also not in this change: moving an event to another calendar, deleting a whole
series in one call, Reminders, and Contacts writes.

## Capabilities

### New Capabilities

- `calendar-write-tools`: `calendar_event_create`, `calendar_event_update` and
  `calendar_event_delete`; who may use them; their arguments, results and limits; single
  occurrence edits and deletes; conditional requests; no scheduling messages; scope; and the
  shared write rate limit.

### Modified Capabilities

- `read-only-and-egress`: "No tool changes iCloud data" allows the calendar write tools to
  change events; "Read-only DAV methods" is narrowed to the DAV base and CardDAV; a new
  "Calendar write DAV methods" requirement lists exactly what may be sent; "Calendar and
  Contacts stay read-only" becomes "Contacts stay read-only"; throttling covers writes.
- `accounts-and-auth`: the `PV_ICLOUD_CALENDAR_WRITE` switch; the per-token
  `calendar_write` key and its validation; `max_writes_per_hour` also applies to calendar
  writes.
- `tool-conventions`: the tool set adds calendar write tools for granted tokens; their
  annotations; instructions for a token with calendar write; audit lines and redaction for
  calendar writes.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `cal/service.py`,
  `cal/caldav.py` (read-only additions: ETag and `calendar-multiget` without expansion, the
  principal's `calendar-user-address-set`), and a new `cal/caldav_write.py` (the only module
  that sends `PUT` or `DELETE`) and `cal/compose.py` (builds and edits iCalendar data).
  `dav.py`, `contacts/`, `mail/` and `people.py` are not changed.
- Tests: `tests/fakecaldav.py` gains `PUT`, `DELETE`, ETags, `If-Match` and
  `If-None-Match` with HTTP 412, a read-only shared calendar (HTTP 403), `calendar-multiget`
  without expansion, the principal's address set, and a record of the scheduling messages
  iCloud would have sent; `tests/test_repo_policy.py` gains static checks that `PUT` and
  `DELETE` appear only in `cal/caldav_write.py`.
- Config: `config/pv-icloud.example.yaml` documents the switch and `calendar_write`, off.
- Dependencies: none new (`icalendar` and `python-dateutil` are already used).
- Egress: unchanged hosts. CalDAV requests may now be `PUT` and `DELETE` to the account's own
  CalDAV host.
- Security posture: a token with calendar write can now create, change and delete events in
  the account's in-scope calendars. Prompt injection from untrusted mail or event text can now
  cause such changes; see `design.md` for mitigations.
- Compatibility: no breaking change. Existing configs keep working and behave exactly as
  today.
