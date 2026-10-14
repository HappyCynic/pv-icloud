# Proposal

## Why

Implements #24. Account owners can read their iCloud calendars through pv-icloud but cannot
change them, so any scheduling change worked out with an agent has to be redone by hand in
Apple's apps. As with mail writes (ADR-0004), different clients deserve different trust, so
calendar writes have to be granted per bearer token and be off unless an operator turns them
on. The rules for this were decided in ADR-0005 (merged in #28), after live checks on iCloud.

## What Changes

- **This change ends the read-only guarantee for Calendar events,** within ADR-0005. CalDAV
  gains two write methods, `PUT` and `DELETE`, sent only from a new write module,
  `cal/caldav_write.py`, and only for a granted calendar write tool call. The DAV base
  (`dav.py`) keeps refusing every method but `PROPFIND` and `REPORT`, so CardDAV and every
  calendar read stay exactly as they are. No new outbound host is added.
- **Deployment switch:** `PV_ICLOUD_CALENDAR_WRITE=enabled` (default off), parsed like
  `PV_ICLOUD_MAIL_WRITE`. Without it the write module is never imported, no calendar write
  tool is registered, and a token granted calendar write is a config error.
- **One per-token switch:** `calendar_write: true` (default `false`). A token without it sees
  exactly today's tools, annotations and instructions.
- Three new tools, all listed only to tokens with `calendar_write`:
  - `calendar_event_create`: one single or recurring event with title, start and end (timed
    or all-day dates), time zone, location, notes and alerts.
  - `calendar_event_update`: change any of those fields on one event; for an occurrence of a
    recurring series, on that occurrence only (the others are unchanged).
  - `calendar_event_delete`: delete one event, or one occurrence of a series.
- **No email to anyone.** iCloud ignores `SCHEDULE-AGENT=CLIENT` (live check, ADR-0005), so
  the tools take **no attendees**, and update and delete are refused for any event or
  occurrence that has an attendee other than the account itself, or that someone else
  organised. Every write also sends `Schedule-Reply: F`. Alerts are written only as display
  alerts. This narrows #24's acceptance criterion "with attendees", as the maintainer decided.
- **Safe concurrent edits.** Create uses `If-None-Match: *`. Update and delete first re-read
  the event resource and send `If-Match` with that ETag; if the event changed meanwhile in
  Apple's apps (or an attendee's reply changed it on the server), iCloud answers 412 and the
  call fails with "the event changed in Calendar; read it again". Never retried.
- **Only calendars the account may change.** A write reaches only an in-scope calendar of the
  caller's account that is not subscribed, not protected, and whose
  `current-user-privilege-set` includes `write-content` (the account's own calendars and
  calendars shared with it read-write). Anything else fails with the same `unknown calendar`
  error as a missing calendar, so nothing leaks about other calendars.
- **Protected calendars:** `calendar.write.protected_calendars` in the defaults and per
  account, plus a token's own `protected_calendars`; the lists add up, names are exact. A
  protected calendar is readable but never written.
- **Limits:** one event per call; the token's existing hourly write limit
  (`max_writes_per_hour`, one budget shared with mail writes); field length caps; iCloud's
  throttling back-off applies to writes, and a throttled write is never retried.
- **Audit:** every write is audit-logged with `write: true` and no event content: titles,
  notes, locations, times, time zones and calendar names are all redacted.
- **Read fix:** iCloud rewrites the account's own address in `ORGANIZER`/`ATTENDEE` to its
  principal path with the address in an `EMAIL=` parameter. The read tools today show
  `email: null` for such a value; they will show the `EMAIL=` address instead.
- Docs: README.md, docs/design.md, CONTRIBUTING.md, SECURITY.md (if it still calls Calendar
  read-only) and `config/pv-icloud.example.yaml` are updated with the implementation.
  CLAUDE.md, `.github/CODEOWNERS`, `openspec/config.yaml` and ADR-0005 already carry the rules
  (#28) and are not changed.

Out of scope (from #24 and ADR-0005): attendees and invitations or replies; subscribed
(webcal) calendars, which stay read-only; creating or deleting calendars; whole-series editing
modes such as "this and following" or changing a series' recurrence rule (the series edit
modes issue). Also not in this change: moving an event to another calendar, deleting a whole
series in one call, Reminders, and Contacts writes (ADR-0006, a separate change).

## Capabilities

### New Capabilities

- `calendar-write-tools`: `calendar_event_create`, `calendar_event_update` and
  `calendar_event_delete`; who may use them; their arguments, results and limits; single
  occurrence edits and deletes; conditional requests; no attendees and no scheduling messages;
  writable calendars and protected calendars; and the shared write rate limit.

### Modified Capabilities

- `read-only-and-egress`: "No tool changes iCloud data" allows the calendar write tools to
  change events; "Read-only DAV methods" is narrowed to the DAV base and CardDAV; a new
  "Calendar write DAV methods" requirement lists exactly what may be sent; "Calendar and
  Contacts stay read-only" becomes "Contacts stay read-only"; throttling covers writes.
- `accounts-and-auth`: the `PV_ICLOUD_CALENDAR_WRITE` switch; calendar write settings per
  account (`calendar.write.protected_calendars`); the per-token `calendar_write` and
  `protected_calendars` keys and their validation; `max_writes_per_hour` also applies to
  calendar writes.
- `tool-conventions`: the tool set adds calendar write tools for granted tokens; their
  annotations; instructions for a token with calendar write; audit lines and redaction for
  calendar writes.
- `calendar-tools`: organiser and attendee `email` taken from the `EMAIL=` parameter when
  iCloud stores a principal path instead of a `mailto:` address.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `cal/service.py`, `cal/parse.py`
  (the `EMAIL=` read fix), `cal/caldav.py` (read-only additions: one event resource with its
  ETag, each calendar's `current-user-privilege-set`, the principal's
  `calendar-user-address-set`), and new `cal/caldav_write.py` (the only module that sends
  `PUT` or `DELETE`) and `cal/compose.py` (builds and edits iCalendar data). `dav.py`,
  `contacts/`, `mail/` and `people.py` are not changed.
- Tests: `tests/fakecaldav.py` gains `PUT`, `DELETE`, ETags, `If-Match` and `If-None-Match`
  with HTTP 412, privilege sets per calendar (own, shared read-write, shared read-only,
  subscribed), principal-path `ORGANIZER`/`ATTENDEE` values with `EMAIL=`, and a record of the
  scheduling messages iCloud would send; `tests/test_repo_policy.py` gains static checks that
  `PUT` and `DELETE` appear only in `cal/caldav_write.py`.
- Config: `config/pv-icloud.example.yaml` documents the switch, `calendar_write` (off) and
  `protected_calendars`.
- Dependencies: none new (`icalendar` and `python-dateutil` are already used).
- Egress: unchanged hosts. CalDAV requests may now be `PUT` and `DELETE` to the account's own
  CalDAV host.
- Security posture: a token with calendar write can now create, change and delete events in
  the account's writable in-scope calendars, including calendars shared read-write, where a
  change is visible to everyone sharing it. Prompt injection from untrusted mail or event text
  can now cause such changes; see `design.md` for mitigations.
- Compatibility: no breaking change. Existing configs keep working and behave exactly as
  today, except the read-tool fix above (an address where `null` was shown before).
