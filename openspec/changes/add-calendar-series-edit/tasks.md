# Tasks

Implements #26. Do not start until the maintainer has approved the proposal, answered the
open questions in design.md, and merged ADR-0007 (design decision 9). Each group ends with its
tests passing.

## 1. Prerequisite

- [ ] 1.1 Confirm that `docs/adr/0007-*.md` exists on the default branch and accepts series
  edits as in design decision 9; if it does not, or decides differently, stop and comment on
  the pull request instead of implementing. Fold the answers to design.md's open questions
  into design.md and the spec deltas, and verify with
  `openspec validate add-calendar-series-edit --strict`.

## 2. Fake CalDAV server

- [ ] 2.1 Extend `tests/fakecaldav.py` where needed: `<C:expand>` honours `COUNT` and `UNTIL`
  (UTC for zoned series, dates for all-day), re-keyed overrides and `STATUS:CANCELLED`
  overrides; a hook that answers the next `PUT` to a given resource with a chosen status
  (412, 404, 403) or drops the answer (no response), so the second write of a split can fail;
  and every request is recorded in order (method, path, preconditions). Verify the existing
  calendar tests still pass.

## 3. Series edits in iCalendar composition

- [ ] 3.1 Write failing tests in `tests/test_cal_compose.py` for: finding the series from any
  occurrence id (master or override); refusing single events, `RDATE`, `EXRULE`, several
  `RRULE`s, `RANGE=THISANDFUTURE` and a lone override; the new definition (fields kept when
  not given, `start`/`end` as the first changed occurrence, the new `DTSTART` as the first
  occurrence of the new rule on or after the anchor, no stray `DTSTART` occurrence, a
  definition with no occurrence refused, `SEQUENCE`/`LAST-MODIFIED`, `TZID` and
  `VTIMEZONE`); `nothing to change` for `update_*` and a reset for `replace_*`; input checks
  for `mode` and `effective`.
- [ ] 3.2 Write failing tests for exceptions: classification (`EXDATE`, override,
  `STATUS:CANCELLED`), the side of the date by original start in the series' zone, discard in
  `replace_*`, match by day and re-key in `update_*` (new time of day, new weekdays, more than
  one occurrence that day), field propagation to overrides (inherited vs. own values, moved
  vs. unmoved times, `all_day` change), and the `unmatched` entries.
- [ ] 3.3 Write failing tests for the split: cutting with `UNTIL` (zoned, all-day) and with
  `COUNT`, earlier overrides and `X-APPLE-*` properties kept byte for byte, later exceptions
  removed from the old part, the new resource (new UUID, `SEQUENCE` 0, no `ORGANIZER` or
  `ATTENDEE`, rest of the old rule or the new one, kept exceptions with the new `UID`), and
  the "no occurrence before / on or after" errors.
- [ ] 3.4 Write failing tests for counting: `occurrences` after deleted ones are removed,
  bounded series, endless series through 366 days with `occurrences_through`, and the
  `exceptions` counts.
- [ ] 3.5 Implement series edits in `src/pv_icloud/cal/compose.py` (pure functions, reusing
  `find_target`, `check_people`, `_like`, `_touch` and `cal/expand.py`) and verify 3.1–3.4
  pass.

## 4. Calendar service

- [ ] 4.1 Write failing tests in `tests/test_calendar_write.py` against the fake server for
  all four modes on a school-day series with deleted and moved occurrences before and after
  the date: what `calendar_events` returns afterwards, occurrences before the date identical
  to before the call in `replace_from` and `update_from`, unmatched exceptions listed, counts,
  the result's `id` readable by `calendar_event`, and `previous` in the `_from` modes.
- [ ] 4.2 Write failing tests for the request sequence: one `PUT` with `If-Match` for `_all`;
  `PUT` with `If-None-Match: *` then `PUT` with `If-Match` for `_from`; rollback (one
  `REPORT`, one `DELETE` with `If-Match`) after 412, 404 and 403 on the second `PUT`, leaving
  the calendar as it was; no rollback and the "may be stored twice" error naming the new id
  after a lost answer, a 503 or a failed rollback; no retries; at most three write requests;
  `preview` sending only reads; `Schedule-Reply: F` on every write and an empty
  `scheduling_messages` record; refusals for attendees, other organisers, protected,
  subscribed and read-only calendars with no write request.
- [ ] 4.3 Implement `CalendarService.update_series` in `src/pv_icloud/cal/service.py` (fresh
  read via `_fresh`, compose, preview, the write order and rollback of design decision 7,
  through the existing `CalDavWriter`) and verify 4.1–4.2 pass.

## 5. Tool, instructions and audit

- [ ] 5.1 Write failing tests in `tests/test_server.py`: `calendar_series_update` is listed
  only to tokens with `calendar_write` and the switch on, with `destructiveHint` true; a token
  without the grant gets the unknown-tool error and iCloud sees no request; the description
  names the four modes, `preview`, discarded and unmatched exceptions, the hourly limit,
  refused attendees and that untrusted content is never a reason; the instructions for a
  `calendar_write` token mention the series tool and confirming with the user; the call
  counts once against `max_writes_per_hour`, a preview included; the audit line has
  `write: true`, redacts every argument (`mode`, `effective`, `preview` included) and holds
  no title, time, zone or calendar name; unknown arguments such as `attendees` are refused.
  Update `tests/snapshots/read_only_surface.json` only if a read-only token's surface
  changes (it must not).
- [ ] 5.2 Register the tool in `src/pv_icloud/server.py` (`CALENDAR_WRITE_TOOLS`,
  description, `CALENDAR_WRITE_TEXT`, unknown-argument guard), say in
  `calendar_event_update`'s and `calendar_event_delete`'s descriptions that whole-series
  changes use `calendar_series_update`, and verify 5.1 passes.

## 6. Docs

- [ ] 6.1 Update docs/design.md (Tools table; the calendar write section's "A whole series is
  never changed or deleted in one call"; a series-edit paragraph on the modes, matching
  exceptions, the split and its rollback; the recurrence notes), README.md (tool list and
  what a series edit can discard), SECURITY.md if it lists what a calendar write can change,
  and add a CHANGELOG.md `Unreleased` entry for `calendar_series_update`. Verify the docs name
  the tool and the four modes consistently with the spec.

## 7. Checks

- [ ] 7.1 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## 8. Live check (maintainer, before merge)

- [ ] 8.1 On the pv-icloud test calendar, with a test token: create a weekly series, delete
  one occurrence and move another (in Calendar), then run each mode (preview first). Confirm
  in iCloud web and Apple's apps that earlier occurrences are unchanged, that both parts of a
  split series display and can be edited, and that iCloud accepts the cut rule and the
  re-keyed overrides. Any difference from the fake is fixed in the fake as well as the server.
