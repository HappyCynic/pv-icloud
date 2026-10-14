# Tasks

Implements #24 under ADR-0005 (rules merged in #28). Do not start until the maintainer has
approved the proposal. Each group ends with its tests passing.

## 1. Policy tests

- [x] 1.1 Write failing tests in `tests/test_repo_policy.py`: `"PUT"` and `"DELETE"` are used
  as HTTP methods (arguments to httpx `request`/`put`/`delete` calls and method constants)
  only in `src/pv_icloud/cal/caldav_write.py`; `dav.READ_METHODS` is exactly `PROPFIND` and
  `REPORT`; no source uses `MKCALENDAR`, `MKCOL`, `PROPPATCH`, `MOVE`, `COPY`, `ACL`, `LOCK`
  or `POST` as a DAV method; no source under `src/pv_icloud/cal/` writes an `ORGANIZER` or
  `ATTENDEE` property; `src/pv_icloud/contacts/` sends nothing but through the DAV base;
  CODEOWNERS lists `/src/pv_icloud/cal/caldav_write.py`. Verify they fail only for the
  missing module.

## 2. Deployment switch, configuration and auth

- [x] 2.1 Write tests in `tests/test_units.py` for `PV_ICLOUD_CALENDAR_WRITE`: unset, empty
  and `disabled` are off; `enabled` is on; other values are a config error naming the
  variable; `calendar_write: true` while off is a config error naming account, label,
  `calendar_write` and the variable; the mail and calendar switches are independent.
- [x] 2.2 Write tests for the token keys `calendar_write` (default `false`; `true` loads;
  lists, strings and numbers rejected with account, label and key) and
  `protected_calendars` (default empty; a list of strings loads; a string or non-string
  items rejected).
- [x] 2.3 Write tests for `calendar.write.protected_calendars` in the defaults and per
  account: the lists add up, an account list never replaces the defaults, names are kept
  exactly, a wrong type or an unknown key under `calendar.write` is an error naming the
  account and key, and it loads with the switch off.
- [x] 2.4 Implement in `src/pv_icloud/config.py` (`TokenEntry.calendar_write`,
  `TokenEntry.protected_calendars`, the account's calendar write settings,
  `Config.calendar_write`, the switch) and verify 2.1–2.3 pass.
- [x] 2.5 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own `calendar_write` and `protected_calendars`; extend `src/pv_icloud/auth.py` and verify it
  passes.
- [x] 2.6 Document the switch, `calendar_write` (off), `protected_calendars` and
  `calendar.write.protected_calendars` in `config/pv-icloud.example.yaml` and verify the
  example still loads in the config tests.

## 3. Fake CalDAV server

- [x] 3.1 Extend `tests/fakecaldav.py`: an ETag per event resource that changes on every
  change; a single-href `calendar-multiget` on the calendar returning the stored data and
  `getetag` (a Depth 0 `calendar-query` on a resource URL answered 403, as on iCloud);
  `calendar-query` with `<C:expand>` expanding stored `RRULE` masters, `EXDATE`s and
  overrides into instances; `PUT` with `If-None-Match: *` (412 when the name exists) or
  `If-Match` (412 on mismatch) and `DELETE` with `If-Match`, rejecting either without a
  precondition; a hook to change an event between a read and a write (as an attendee reply
  would); `current-user-privilege-set` per calendar (own and shared read-write with
  `write-content`, shared read-only and subscribed without); the principal's
  `calendar-user-address-set`; organiser and attendee values stored as the principal path
  with `EMAIL=`, as iCloud does; 503 with `Retry-After` on demand for writes; and a
  `scheduling_messages` list recording what iCloud would send (any `PUT` or `DELETE` of an
  event with an attendee that is not the account, whatever its `SCHEDULE-AGENT`). Writes to
  subscribed calendars, calendars without `write-content`, collections or CardDAV URLs are
  recorded as forbidden write attempts. Verify the existing calendar tests still pass.

## 4. Read-side additions and the `EMAIL=` fix

- [x] 4.1 Write tests in `tests/test_calendar.py`: an organiser or attendee stored as a
  principal path with `EMAIL=` shows that address as `email` in `calendar_events` and
  `calendar_event`, and `calendar_search` finds it; without `EMAIL=` `email` stays null.
- [x] 4.2 Fix `_person` in `src/pv_icloud/cal/parse.py`; verify 4.1 passes. Add a
  `CHANGELOG.md` `Unreleased` entry for the fix.
- [x] 4.3 Write tests for the new reads in `cal/caldav.py`: the calendar list carries each
  calendar's privileges (from `current-user-privilege-set`, asked for in the existing
  `PROPFIND`, with no extra request); one event resource read with its ETag, unexpanded;
  the account's calendar addresses and principal URL read once and cached. All send only
  `PROPFIND` and `REPORT`.
- [x] 4.4 Implement them in `src/pv_icloud/cal/caldav.py`; verify 4.3 passes and the read
  tools' results are unchanged.

## 5. iCalendar composition

- [x] 5.1 Write tests for `src/pv_icloud/cal/compose.py`: new timed event with `TZID` and
  `VTIMEZONE` (offset converted, no offset read in the zone, default zone); all-day event
  with inclusive `end`; default ends; `RRULE` from `recurrence`; alerts as `ACTION:DISPLAY`
  `VALARM`s; no `ORGANIZER` or `ATTENDEE` ever written; update of given fields only and
  `clear`; override created for an occurrence (copied from the master without
  `RRULE`/`RDATE`/`EXDATE`, `RECURRENCE-ID` with the master's value type and `TZID`) or an
  existing override edited; occurrence delete as `EXDATE` with the master's value type and
  `TZID`, removing its override; `SEQUENCE` and `LAST-MODIFIED` bumped; unknown components
  and `X-APPLE-*` properties kept on round trip; the "account itself" check (`mailto:` in the
  address set, principal path with a matching `EMAIL=` or equal to the principal URL,
  case-insensitive; anything else is someone else); every input limit in the
  `calendar-write-tools` spec refused with the field named.
- [x] 5.2 Implement `src/pv_icloud/cal/compose.py` (SPDX header) with `icalendar`, sharing
  the address helper with `cal/parse.py`; verify 5.1 passes.

## 6. Write path

- [x] 6.1 Write tests for `src/pv_icloud/cal/caldav_write.py` against the fake: only `PUT`
  and `DELETE`; refuses to send without `If-Match` or `If-None-Match: *`; refuses any URL
  that is not a direct `.ics` child of the given writable calendar collection; sends
  `Schedule-Reply: F`; 412 mapped to `the event changed in Calendar; read it again` and not
  retried; 404 to `not found`; 403 and other 4xx to `iCloud refused the change`; 429/503
  pauses the account's shared `Throttle` and is not retried; a paused account sends nothing;
  a timeout after sending reports an unconfirmed change; logs carry no path, UID or body.
- [x] 6.2 Implement `src/pv_icloud/cal/caldav_write.py` (SPDX header); verify 6.1 and 1.1
  pass.

## 7. Create, update and delete in the calendar service

- [x] 7.1 Write tests in a new `tests/test_calendar_write.py`: create (timed, all-day,
  recurring, with alerts) readable at once by `calendar_event` with the returned id; update
  of one field keeps the rest; edit and delete of one occurrence leave the others unchanged;
  delete of a single event; every update and delete re-reads the resource and sends that
  ETag (a stale ETag from an earlier call is never used); conflict (412) leaves the other
  version and sends one `PUT` only; event or occurrence gone (`not found`, no write);
  excluded, missing, other-account, subscribed, protected (defaults, account and token) and
  read-only shared calendars all fail with the same `unknown calendar` error and send
  nothing, while a read-write shared calendar accepts writes; protected and read-only
  calendars stay readable; events organised by someone else, events or series masters with
  an attendee other than the account, and principal paths without a known `EMAIL=` refused
  before any write; events whose only organiser and attendee are the account's principal
  path allowed; `scheduling_messages` stays empty in every test.
- [x] 7.2 Implement create, update and delete in `src/pv_icloud/cal/service.py`; verify 7.1
  passes.

## 8. Tools, permissions and audit

- [x] 8.1 Write tests in `tests/test_server.py`: with the switch off, `cal.caldav_write` and
  `cal.compose` are not imported and no calendar write tool is registered; a token without
  `calendar_write` gets the same `tools/list`, annotations and instructions as before
  (snapshot); a token with it sees the three tools with the annotations in the spec and no
  `attendees` argument; a call from a token without it fails as unknown with no CalDAV
  request; mail and calendar grants do not imply each other; a token's
  `protected_calendars` apply to that token only; the shared hourly write limit counts mail
  and calendar calls together and refuses with `outcome` `throttled` before any request;
  instructions for calendar-only, mail-only and combined tokens match the spec; descriptions
  state the limits, opt-in, one event per call, shared calendars, no attendees, protected
  and subscribed calendars read-only, and the untrusted-content warning; audit lines have
  `write: true` and no title, notes, location, address, time, zone or calendar name,
  including for refused calls.
- [x] 8.2 Implement the three tools, conditional registration behind the switch, per-token
  `tools/list` filtering and call-time checks, instructions, and calendar-write audit
  redaction in `src/pv_icloud/server.py`; verify 8.1 passes.

## 9. Docs and checks

- [ ] 9.1 Update README.md (switch, tools, config including `protected_calendars`, shared
  calendars, the no-attendees rule), docs/design.md (Goals, Tools table, a "Calendar writes"
  section on writable calendars, fresh reads and conditional writes, occurrence edits and why
  attendees are refused), CONTRIBUTING.md and SECURITY.md wherever they still say Calendar is
  read-only, and docs/egress.md if it describes CalDAV methods; add `Unreleased` entries to
  CHANGELOG.md for the switch, `calendar_write`, `protected_calendars` and the three tools.
- [ ] 9.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## 10. Live check (maintainer, before merge)

- [ ] 10.1 On the pv-icloud test calendar, with a test token: create a recurring event;
  edit one occurrence (a `RECURRENCE-ID` override) and delete another (`EXDATE`); confirm in
  Calendar that the other occurrences are unchanged, and that the single-href
  `calendar-multiget` re-read (design decision 5) is what precedes each write. Any difference
  from the fake is fixed in the fake as well as the server.
