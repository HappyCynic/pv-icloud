# Tasks

Implements #26. Do not start until the maintainer has approved the proposal and merged
ADR-0007 (design decision 11). Each group ends with its tests passing.

## 1. Prerequisite

- [x] 1.1 Confirm that `docs/adr/0007-*.md` exists on the default branch and decides what
  design decision 11 lists (series edits, the Apple-format split and its rollback `DELETE`,
  recurrence sets of up to 10 parts found by time-range listings without property filters);
  if it does not, or decides differently, stop and comment on the pull request instead of
  implementing. Fold any answer to design.md's remaining open questions into design.md and
  the spec deltas; verify with `openspec validate add-calendar-series-edit --strict`.

## 2. Fake CalDAV server

- [x] 2.1 Extend `tests/fakecaldav.py` where needed: `<C:expand>` honours `COUNT` and `UNTIL`
  (UTC for zoned series, local for floating, dates for all-day), re-keyed overrides and
  `STATUS:CANCELLED` overrides; an unexpanded `calendar-query` with a `time-range` that may
  have no end (a recurring resource matches when any occurrence falls in it), returning
  `getetag` and full data; iCloud's answers to filters it does not support (207 with no
  matches for any `prop-filter`, 412 for a `collation`, 500 for a `param-filter`, partial
  `calendar-data` ignored); a hook that answers the next `PUT` to a given
  resource with a chosen status (412, 404, 403, 503) or drops the answer (no response), so
  any write of a series edit can fail; and every request is recorded in order (method, path,
  preconditions). Verify the existing calendar tests still pass.

## 3. Series edits in iCalendar composition

- [x] 3.1 Write failing tests in `tests/test_cal_compose.py` for: finding the series from any
  occurrence id (master or override); refusing single events, `RDATE`, `EXRULE`, several
  `RRULE`s, `RANGE=THISANDFUTURE` and a lone override; the new definition (fields kept when
  not given, `start`/`end` as the first changed occurrence, the new `DTSTART` as the first
  occurrence of the new rule on or after the anchor, no stray `DTSTART` occurrence, a
  definition with no occurrence refused, `SEQUENCE`/`LAST-MODIFIED`, `TZID` and
  `VTIMEZONE`); pattern fields vs. field edits; `nothing to change` for `update_*` and a reset
  for `replace_*`; input checks for `mode`, `effective`, `drop_unmatched` and `preview`.
- [x] 3.2 Write failing tests for exceptions: classification (`EXDATE`, override,
  `STATUS:CANCELLED`), the side of the date by original start in the series' zone, discard in
  `replace_*`, match by day and re-key in `update_*` (new time of day, new weekdays, more than
  one occurrence that day), field propagation to overrides (inherited vs. own values, as in
  the maintainer's notes example; moved vs. unmoved times; `all_day` change), the `unmatched`
  entries, unmatched deleted occurrences dropped, unmatched changed occurrences refused
  without `drop_unmatched` (error holds only the count) and dropped with it, and
  `requires_drop_unmatched` in a preview.
- [x] 3.3 Write failing tests for the split in Apple's format: `UNTIL` at 23:59:59 local on
  the day before `effective` in UTC (`20261103T055959Z` for America/Chicago and 2026-11-03,
  and a date across a DST change), floating and all-day (`DATE` of the day before) forms,
  `COUNT` replaced by `UNTIL` on
  the old part and the remaining `COUNT` on the new one; earlier overrides and `X-APPLE-*`
  properties kept property for property (the iCalendar writer orders properties its own way,
  as for every calendar write); later exceptions removed from the old part; the new resource
  (new UUID, `SEQUENCE` 0, every master property and alarm copied, `LOCATION`,
  `X-APPLE-STRUCTURED-LOCATION` and `TRANSP` included, no `ORGANIZER` or `ATTENDEE`, rest of
  the old rule or the new one, kept exceptions with the new `UID`); the recurrence-set
  `RELATED-TO` generated (unrelated to either `UID`) or reused, on both masters and on every
  override created or re-keyed (set to the master's value, removed when the master has
  none), with the cut part's remaining overrides left as stored; the "no occurrence on or
  after" error; and `effective` on or before the first occurrence changing the part in place
  (`parts.split` false), with or without a recurrence set.
- [x] 3.4 Write failing tests for recurrence sets: picking parts out of a listing by their
  masters' `RELATED-TO` value and `RELTYPE` (other events, overrides and other values
  ignored), ordering by first occurrence, the 10-part cap, field edits applied per part,
  refusals of `replace_all`, of pattern changes in `update_all`, and of pattern changes and
  `replace_from` with later parts (with the date in the message), the part containing
  `effective`, and `effective` on or before a part's first occurrence changing that part in
  place; the listing windows (later parts from the named part's last occurrence or from
  `effective`, none for an endless named part in the `_all` modes; earlier parts from 366
  days before today to the named part's first occurrence, `_all` modes only, skipped when
  that is before the window) and `searched_from`.
- [x] 3.5 Write failing tests for counting: `occurrences` after deleted ones are removed,
  bounded series, endless series through 366 days with `occurrences_through`, counts over
  several parts, `parts`, and the `exceptions` counts.
- [x] 3.6 Implement series edits in `src/pv_icloud/cal/compose.py` (pure functions, reusing
  `find_target`, `check_people`, `_like`, `_touch` and `cal/expand.py`) and verify 3.1–3.5
  pass.

## 4. Calendar service

- [x] 4.1 Write failing tests in `tests/test_calendar_write.py` against the fake server for
  all four modes on a school-day series with deleted and moved occurrences before and after
  the date: what `calendar_events` returns afterwards, occurrences before the date identical
  to before the call in `replace_from` and `update_from`, unmatched exceptions listed and
  `drop_unmatched`, counts, the result's `id` readable by `calendar_event`, and `previous` and
  `ids_changed_from` in the `_from` modes.
- [x] 4.2 Write failing tests for split series against the fake server: a two-part set
  changed with `update_all` and a new title (one `PUT` with `If-Match` per part, both parts
  show the title); the refusals with no write request; `update_from` with a later part
  (split, then the later part's `PUT`); `effective` at a later part's start (in place, no new
  resource); a later part answered 412 (error names changed and failed parts, nothing more
  sent, nothing undone); an invited later part refused; 11 parts refused; parts in another
  calendar ignored; a part that ended more than a year ago not written by `update_all`; the
  `_from` modes never listing before `effective`; and no request with a `prop-filter`,
  `param-filter` or `collation` in any test.
- [x] 4.3 Write failing tests for the request sequence: one `PUT` with `If-Match` per part for
  `_all`; `PUT` with `If-None-Match: *` then `PUT` with `If-Match` for a split; rollback (one
  `REPORT`, one `DELETE` with `If-Match`) after 412, 404 and 403 on the cut, leaving the
  calendar as it was, and no later part written; no rollback and the "may be stored twice"
  error naming the new id after a lost answer, a 503 or a failed rollback; no retries; at
  most one write per part plus two; only `REPORT`s to find and read parts; `preview` sending
  only reads; `Schedule-Reply: F` on every write and an empty `scheduling_messages` record;
  refusals for attendees, other organisers, protected, subscribed and read-only calendars
  with no write request.
- [x] 4.4 Add the unexpanded time-range listing to `src/pv_icloud/cal/caldav.py` (a
  `calendar-query` `REPORT` through the read-only DAV base, optional end, no property
  filter), implement `CalendarService.update_series` in
  `src/pv_icloud/cal/service.py` (fresh reads via `_fresh`, compose, preview, the write order
  and rollback of design decision 9, part-by-part writes, through the existing
  `CalDavWriter`) and verify 4.1–4.3 pass.

## 5. Tool, instructions and audit

- [x] 5.1 Write failing tests in `tests/test_server.py`: `calendar_series_update` is listed
  only to tokens with `calendar_write` and the switch on, with `destructiveHint` true; a token
  without the grant gets the unknown-tool error and iCloud sees no request; the description
  names the four modes, `preview`, `drop_unmatched`, discarded and unmatched exceptions, that
  occurrence ids from a split date change, the 10-part limit, that parts that ended more than
  a year ago are not changed, the hourly limit, refused
  attendees and that untrusted content is never a reason; the instructions for a
  `calendar_write` token mention the series tool, confirming with the user and changed ids;
  the call counts once against `max_writes_per_hour`, a preview included; the audit line has
  `write: true`, redacts every argument (`mode`, `effective`, `drop_unmatched`, `preview`
  included) and holds no title, time, zone or calendar name; unknown arguments such as
  `attendees` are refused. Update `tests/snapshots/read_only_surface.json` only if a
  read-only token's surface changes (it must not).
- [x] 5.2 Register the tool in `src/pv_icloud/server.py` (`CALENDAR_WRITE_TOOLS`,
  description, `CALENDAR_WRITE_TEXT`, unknown-argument guard), say in
  `calendar_event_update`'s and `calendar_event_delete`'s descriptions that whole-series
  changes use `calendar_series_update`, and verify 5.1 passes.

## 6. Docs

- [ ] 6.1 Update docs/design.md (Tools table; the calendar write section's "A whole series is
  never changed or deleted in one call"; a series-edit paragraph on the modes, matching
  exceptions and `drop_unmatched`, the Apple-format split and its rollback, recurrence sets
  and their limits; the recurrence notes), README.md (tool list and what a series edit can
  discard), SECURITY.md if it lists what a calendar write can change, and add a CHANGELOG.md
  `Unreleased` entry for `calendar_series_update`. Verify the docs name the tool and the four
  modes consistently with the spec.

## 7. Checks

- [ ] 7.1 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## 8. Live check (maintainer, before merge)

- [ ] 8.1 On the pv-icloud test calendar, with a test token: create a weekly series, delete
  one occurrence, move one and retitle one (in Calendar), then run each mode (preview first),
  and a field edit on a series split in Calendar. Confirm in iCloud web and Apple's apps that
  earlier occurrences are unchanged, that a split made by pv-icloud shows and edits as one
  series ("All Future Events" from the first occurrence changes both parts), and that iCloud
  accepts the cut rule (including an all-day `DATE` `UNTIL`) and the re-keyed overrides with
  their recurrence-set `RELATED-TO`. Any difference from the fake is fixed in the fake as well
  as the server.
