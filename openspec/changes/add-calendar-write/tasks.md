# Tasks

Implements #24. Do not start until the maintainer has approved the proposal, answered open
question 1 in `design.md` (iCloud and `SCHEDULE-AGENT=CLIENT`), and merged the separate
rule-change pull request (ADR-0005, CLAUDE.md DAV hard rule, `.github/CODEOWNERS`,
`openspec/config.yaml`, SECURITY.md). Those rule changes are not tasks of this change. Each
group ends with its tests passing.

## 1. Policy tests

- [ ] 1.1 Write failing tests in `tests/test_repo_policy.py`: `"PUT"` and `"DELETE"` are used
  as HTTP methods (arguments to httpx `request`/`put`/`delete` calls and method constants)
  only in `src/pv_icloud/cal/caldav_write.py`; `dav.READ_METHODS` is exactly `PROPFIND` and
  `REPORT`; no source uses `MKCALENDAR`, `PROPPATCH`, `MOVE`, `COPY` or `POST` as a DAV
  method; `src/pv_icloud/contacts/` sends nothing but through the DAV base; CODEOWNERS lists
  `/src/pv_icloud/cal/caldav_write.py`. Verify they fail only for the missing module.

## 2. Deployment switch, configuration and auth

- [ ] 2.1 Write tests in `tests/test_units.py` for `PV_ICLOUD_CALENDAR_WRITE`: unset, empty
  and `disabled` are off; `enabled` is on; other values are a config error naming the
  variable; `calendar_write: true` while off is a config error naming account, label,
  `calendar_write` and the variable; the mail and calendar switches are independent.
- [ ] 2.2 Write tests for the token key `calendar_write`: default `false`; `true` loads;
  lists, strings and numbers rejected with account, label and key.
- [ ] 2.3 Implement in `src/pv_icloud/config.py` (`TokenEntry.calendar_write`,
  `Config.calendar_write`, the switch) and verify 2.1 and 2.2 pass.
- [ ] 2.4 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own `calendar_write`; extend `src/pv_icloud/auth.py` and verify it passes.
- [ ] 2.5 Document the switch and `calendar_write`, off, in `config/pv-icloud.example.yaml`
  and verify the example still loads in the config tests.

## 3. Fake CalDAV server

- [ ] 3.1 Extend `tests/fakecaldav.py`: ETags per resource; `calendar-multiget` without
  `<C:expand>` returning stored data and `getetag`; `PUT` with `If-None-Match: *` (412 when
  the name exists) or `If-Match` (412 on mismatch) and `DELETE` with `If-Match`, rejecting
  either without a precondition; a calendar shared read-only answering 403; 503 with
  `Retry-After` on demand for writes; the principal's `calendar-user-address-set`; and a
  `scheduling_messages` list recording what a scheduling server would send (an attendee
  without `SCHEDULE-AGENT=CLIENT` on an organiser's `PUT` or `DELETE`, or any change to an
  event organised by someone else without `Schedule-Reply: F`). Writes to subscribed
  calendars, collections or CardDAV URLs are recorded as forbidden write attempts. Verify
  the existing calendar tests still pass.

## 4. Read-side additions in `cal/caldav.py`

- [ ] 4.1 Write tests for reading one stored resource with its ETag by `calendar-multiget`
  without expansion, and for reading and caching the account's calendar addresses; both
  send only `REPORT` and `PROPFIND`.
- [ ] 4.2 Implement them in `src/pv_icloud/cal/caldav.py`; verify 4.1 passes and the read
  tools' request stream is unchanged.

## 5. iCalendar composition

- [ ] 5.1 Write tests for `src/pv_icloud/cal/compose.py`: new timed event with `TZID` and
  `VTIMEZONE` (offset converted, no offset read in the zone, default zone); all-day event
  with inclusive `end`; default ends; `RRULE` from `recurrence`; alerts as `ACTION:DISPLAY`
  `VALARM`s; attendees and `ORGANIZER` with `SCHEDULE-AGENT=CLIENT`; update of given fields
  only and `clear`; override created for an occurrence (copied from the master without
  `RRULE`/`RDATE`/`EXDATE`) or an existing override edited; occurrence delete as `EXDATE`
  with the master's value type and `TZID`, removing its override; `SEQUENCE` and
  `LAST-MODIFIED` bumped; unknown components and `X-APPLE-*` properties kept on round trip;
  every input limit in the `calendar-write-tools` spec refused with the field named.
- [ ] 5.2 Implement `src/pv_icloud/cal/compose.py` (SPDX header) with `icalendar`; verify
  5.1 passes.

## 6. Write path

- [ ] 6.1 Write tests for `src/pv_icloud/cal/caldav_write.py` against the fake: only `PUT`
  and `DELETE`; refuses to send without `If-Match` or `If-None-Match: *`; refuses any URL
  that is not a direct `.ics` child of the given calendar collection; sends
  `Schedule-Reply: F`; 412, 403 and 404 mapped to their tool errors; 429/503 pauses the
  account's shared `Throttle` and is not retried; a paused account sends nothing; a timeout
  after sending reports an unconfirmed change; logs carry no path, UID or body.
- [ ] 6.2 Implement `src/pv_icloud/cal/caldav_write.py` (SPDX header); verify 6.1 and 1.1
  pass.

## 7. Create, update and delete in the calendar service

- [ ] 7.1 Write tests in a new `tests/test_calendar_write.py`: create (timed, all-day,
  recurring, with attendees and alerts) readable at once by `calendar_event` with the
  returned id; update of one field keeps the rest; edit and delete of one occurrence leave
  the others unchanged; delete of a single event; excluded, missing and other-account
  calendars all fail with the same `unknown calendar` error and send nothing; subscribed
  calendar refused; read-only shared calendar refused; event gone (`not found`, no `PUT`);
  conflict (412) leaves the other version; events organised by someone else and events with
  notifying attendees refused before any write; `scheduling_messages` stays empty in every
  test.
- [ ] 7.2 Implement create, update and delete in `src/pv_icloud/cal/service.py`; verify 7.1
  passes.

## 8. Tools, permissions and audit

- [ ] 8.1 Write tests in `tests/test_server.py`: with the switch off, `cal.caldav_write` and
  `cal.compose` are not imported and no calendar write tool is registered; a token without
  `calendar_write` gets the same `tools/list`, annotations and instructions as before
  (snapshot); a token with it sees the three tools with the annotations in the spec; a call
  from a token without it fails as unknown with no CalDAV request; mail and calendar grants
  do not imply each other; the shared hourly write limit counts mail and calendar calls
  together and refuses with `outcome` `throttled` before any request; instructions for
  calendar-only, mail-only and combined tokens match the spec; descriptions state the
  limits, opt-in, one event per call, no invitations, subscribed calendars read-only, and
  the untrusted-content warning; audit lines have `write: true` and no title, notes,
  location, address, time, zone or calendar name.
- [ ] 8.2 Implement the three tools, conditional registration behind the switch, per-token
  `tools/list` filtering and call-time checks, instructions, and calendar-write audit
  redaction in `src/pv_icloud/server.py`; verify 8.1 passes.

## 9. Docs and checks

- [ ] 9.1 Update README.md (switch, tools, config, the no-invitations rule), docs/design.md
  (Goals, Tools table, a "Calendar writes" section on conditional writes, occurrence edits and
  scheduling), CONTRIBUTING.md (wording that says Calendar is read-only), and
  docs/egress.md if it describes CalDAV methods; add `Unreleased` entries to CHANGELOG.md for
  the switch, `calendar_write` and the three tools.
- [ ] 9.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.
