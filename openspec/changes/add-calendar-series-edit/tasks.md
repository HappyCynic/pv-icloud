# Tasks

Implements #26. Do not start until the maintainer has approved the proposal, merged ADR-0007
(design decision 11) and reported the live check of the `RELATED-TO` query (design
decision 6). Each group ends with its tests passing.

## 1. Prerequisite

- [ ] 1.1 Confirm that `docs/adr/0007-*.md` exists on the default branch and decides what
  design decision 11 lists (series edits, the Apple-format split and its rollback `DELETE`,
  recurrence sets of up to 10 parts); if it does not, or decides differently, stop and comment
  on the pull request instead of implementing. Read the maintainer's result of the live check
  of a `calendar-query` `prop-filter` on `RELATED-TO` on the pull request or #26, and fold it
  (filter or calendar listing) and any answer to design.md's remaining open questions into
  design.md and the spec deltas; verify with `openspec validate add-calendar-series-edit
  --strict`.

## 2. Fake CalDAV server

- [ ] 2.1 Extend `tests/fakecaldav.py` where needed: `<C:expand>` honours `COUNT` and `UNTIL`
  (UTC for zoned series, local for floating, dates for all-day), re-keyed overrides and
  `STATUS:CANCELLED` overrides; a `calendar-query` with a `prop-filter`/`text-match` on
  `RELATED-TO` (or, if the live check found iCloud refuses it, the same refusal) and a
  `calendar-query` without a time range; a hook that answers the next `PUT` to a given
  resource with a chosen status (412, 404, 403, 503) or drops the answer (no response), so
  any write of a series edit can fail; and every request is recorded in order (method, path,
  preconditions). Verify the existing calendar tests still pass.

## 3. Series edits in iCalendar composition

- [ ] 3.1 Write failing tests in `tests/test_cal_compose.py` for: finding the series from any
  occurrence id (master or override); refusing single events, `RDATE`, `EXRULE`, several
  `RRULE`s, `RANGE=THISANDFUTURE` and a lone override; the new definition (fields kept when
  not given, `start`/`end` as the first changed occurrence, the new `DTSTART` as the first
  occurrence of the new rule on or after the anchor, no stray `DTSTART` occurrence, a
  definition with no occurrence refused, `SEQUENCE`/`LAST-MODIFIED`, `TZID` and
  `VTIMEZONE`); pattern fields vs. field edits; `nothing to change` for `update_*` and a reset
  for `replace_*`; input checks for `mode`, `effective`, `drop_unmatched` and `preview`.
- [ ] 3.2 Write failing tests for exceptions: classification (`EXDATE`, override,
  `STATUS:CANCELLED`), the side of the date by original start in the series' zone, discard in
  `replace_*`, match by day and re-key in `update_*` (new time of day, new weekdays, more than
  one occurrence that day), field propagation to overrides (inherited vs. own values, as in
  the maintainer's notes example; moved vs. unmoved times; `all_day` change), the `unmatched`
  entries, unmatched deleted occurrences dropped, unmatched changed occurrences refused
  without `drop_unmatched` (error holds only the count) and dropped with it, and
  `requires_drop_unmatched` in a preview.
- [ ] 3.3 Write failing tests for the split in Apple's format: `UNTIL` at 23:59:59 local on
  the day before `effective` in UTC (`20261103T055959Z` for America/Chicago and 2026-11-03,
  and a date across a DST change), floating and all-day forms, `COUNT` replaced by `UNTIL` on
  the old part and the remaining `COUNT` on the new one; earlier overrides and `X-APPLE-*`
  properties kept byte for byte; later exceptions removed from the old part; the new resource
  (new UUID, `SEQUENCE` 0, every master property and alarm copied, `LOCATION`,
  `X-APPLE-STRUCTURED-LOCATION` and `TRANSP` included, no `ORGANIZER` or `ATTENDEE`, rest of
  the old rule or the new one, kept exceptions with the new `UID`); the recurrence-set
  `RELATED-TO` generated (unrelated to either `UID`) or reused, on both masters; and the "no
  occurrence before / on or after" errors.
- [ ] 3.4 Write failing tests for recurrence sets: grouping parts by `RELATED-TO` value and
  `RELTYPE`, ordering by first occurrence, the 10-part cap, field edits applied per part,
  refusals of `replace_all`, of pattern changes in `update_all`, and of pattern changes and
  `replace_from` with later parts (with the date in the message), the part containing
  `effective`, and `effective` at a later part's start changing that part in place.
- [ ] 3.5 Write failing tests for counting: `occurrences` after deleted ones are removed,
  bounded series, endless series through 366 days with `occurrences_through`, counts over
  several parts, `parts`, and the `exceptions` counts.
- [ ] 3.6 Implement series edits in `src/pv_icloud/cal/compose.py` (pure functions, reusing
  `find_target`, `check_people`, `_like`, `_touch` and `cal/expand.py`) and verify 3.1–3.5
  pass.

## 4. Calendar service

- [ ] 4.1 Write failing tests in `tests/test_calendar_write.py` against the fake server for
  all four modes on a school-day series with deleted and moved occurrences before and after
  the date: what `calendar_events` returns afterwards, occurrences before the date identical
  to before the call in `replace_from` and `update_from`, unmatched exceptions listed and
  `drop_unmatched`, counts, the result's `id` readable by `calendar_event`, and `previous` and
  `ids_changed_from` in the `_from` modes.
- [ ] 4.2 Write failing tests for split series against the fake server: a two-part set
  changed with `update_all` and a new title (one `PUT` with `If-Match` per part, both parts
  show the title); the refusals with no write request; `update_from` with a later part
  (split, then the later part's `PUT`); `effective` at a later part's start (in place, no new
  resource); a later part answered 412 (error names changed and failed parts, nothing more
  sent, nothing undone); an invited later part refused; 11 parts refused; parts in another
  calendar ignored.
- [ ] 4.3 Write failing tests for the request sequence: one `PUT` with `If-Match` per part for
  `_all`; `PUT` with `If-None-Match: *` then `PUT` with `If-Match` for a split; rollback (one
  `REPORT`, one `DELETE` with `If-Match`) after 412, 404 and 403 on the cut, leaving the
  calendar as it was, and no later part written; no rollback and the "may be stored twice"
  error naming the new id after a lost answer, a 503 or a failed rollback; no retries; at
  most one write per part plus two; only `REPORT`s to find and read parts; `preview` sending
  only reads; `Schedule-Reply: F` on every write and an empty `scheduling_messages` record;
  refusals for attendees, other organisers, protected, subscribed and read-only calendars
  with no write request.
- [ ] 4.4 Add the recurrence-set read to `src/pv_icloud/cal/caldav.py` (a `REPORT` through the
  read-only DAV base, as settled in 1.1), implement `CalendarService.update_series` in
  `src/pv_icloud/cal/service.py` (fresh reads via `_fresh`, compose, preview, the write order
  and rollback of design decision 9, part-by-part writes, through the existing
  `CalDavWriter`) and verify 4.1–4.3 pass.

## 5. Tool, instructions and audit

- [ ] 5.1 Write failing tests in `tests/test_server.py`: `calendar_series_update` is listed
  only to tokens with `calendar_write` and the switch on, with `destructiveHint` true; a token
  without the grant gets the unknown-tool error and iCloud sees no request; the description
  names the four modes, `preview`, `drop_unmatched`, discarded and unmatched exceptions, that
  occurrence ids from a split date change, the 10-part limit, the hourly limit, refused
  attendees and that untrusted content is never a reason; the instructions for a
  `calendar_write` token mention the series tool, confirming with the user and changed ids;
  the call counts once against `max_writes_per_hour`, a preview included; the audit line has
  `write: true`, redacts every argument (`mode`, `effective`, `drop_unmatched`, `preview`
  included) and holds no title, time, zone or calendar name; unknown arguments such as
  `attendees` are refused. Update `tests/snapshots/read_only_surface.json` only if a
  read-only token's surface changes (it must not).
- [ ] 5.2 Register the tool in `src/pv_icloud/server.py` (`CALENDAR_WRITE_TOOLS`,
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
  accepts the cut rule and the re-keyed overrides. Also note Apple's `UNTIL` for all-day and
  floating series, whether Apple writes the recurrence-set `RELATED-TO` on overrides, and
  whether it replaces a `COUNT` on a split. Any difference from the fake is fixed in the fake
  as well as the server.
