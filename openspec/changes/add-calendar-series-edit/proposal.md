# Proposal

## Why

Implements #26. A recurring event such as "every school day this year" collects exceptions:
deleted occurrences (holidays) and changed or moved ones (games, schedule changes). The
calendar write tools (ADR-0005, #24) change one event or one occurrence per call and never a
whole series, so changing the series today means editing every occurrence by hand, or
deleting and recreating it and losing its exceptions.

## What Changes

- **Still opt-in, still no new DAV method.** The change adds one calendar write tool,
  `calendar_series_update`, under the existing `PV_ICLOUD_CALENDAR_WRITE=enabled` switch and
  the token's `calendar_write: true` grant. Every write request is one that ADR-0005 already
  allows: a conditional `PUT` of one event resource (`If-Match` from a fresh read, or
  `If-None-Match: *` for a new resource) or a conditional `DELETE` of one event resource, each
  with `Schedule-Reply: F`, sent from `cal/caldav_write.py` only. The DAV base and every read
  stay read-only; finding the parts of a split series is a `REPORT`. There are no attendees,
  and a series is refused if any part of it has attendees or was organised by someone else,
  as with the existing tools.
- **It goes beyond ADR-0005 decision 3**, which leaves whole-series edit modes out of scope
  and limits every call to one event. The maintainer has accepted this approach on #26 and
  will land **ADR-0007** before implementation. ADR-0007 amends decision 3 and covers splits
  and split series. Design decision 11 lists what this change assumes ADR-0007 decides.
- **Four modes**, as the issue asks (`mode`):
  1. `replace_all`: every occurrence takes the new definition; every deleted and changed
     occurrence is discarded.
  2. `replace_from` + `effective` date: occurrences before the date are untouched; those on or
     after it take the new definition, and exceptions on or after it are discarded.
  3. `update_all`: the series takes the new definition; deleted occurrences stay deleted and
     changed occurrences keep their own changes.
  4. `update_from` + `effective` date: like `update_all`, only on or after the date.
- **The new definition** uses the fields `calendar_event_update` already has (`title`,
  `start`, `end`, `all_day`, `timezone`, `location`, `notes`, `alerts`, `clear`) plus
  `recurrence` (the same object `calendar_event_create` takes). Fields that are not given
  keep the series' current values. A changed field reaches an override only where that
  override still had the series' value, which is what Apple's apps do.
- **Exceptions that no longer fit are always listed and never silently dropped.** In the
  keeping modes an exception follows the new pattern when the new series has an occurrence on
  the same day. Otherwise it goes in the result's `unmatched` list. An unmatched deleted
  occurrence is dropped. An unmatched changed occurrence makes the call fail unless the caller
  passes `drop_unmatched: true`.
- **Counts.** The result reports how many occurrences take the new definition, how many
  exceptions were kept, discarded and unmatched, and how many parts of the series changed.
- **Preview.** `preview: true` works out the change from a fresh read and returns the same
  result without writing anything, including the `unmatched` list and whether
  `drop_unmatched` would be needed. A preview counts against the write limit.
- **"From a date" splits the series the way Apple Calendar does.** The existing resource gets
  `UNTIL` = 23:59:59 local time on the day before the date, written in UTC, so its earlier
  occurrences and exceptions are unchanged. The part from the date on becomes a new series
  with a new `UID` in a new resource in the same calendar, and copies the master's properties.
  Both parts carry the same `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value. The
  new resource is created first. If cutting the old one then fails (for example because it
  changed in Calendar meanwhile), the new resource is deleted again, so the usual failure
  leaves the calendar as it was. Occurrence ids from the date change; the result
  (`ids_changed_from`) and the tool description say so.
- **A series already split in Calendar is one series**, as Apple treats it. It is found by
  its recurrence-set `RELATED-TO`, and a call can change up to 10 parts.
  - `update_all` with only title, location, notes or alerts changes every part, each with its
    own fresh read and `If-Match` `PUT`.
  - `replace_all`, and `update_all` that changes the pattern, are refused on a split series.
    The caller is told to change it from a date instead.
  - The `_from` modes split the part that contains the date and give later parts the field
    edits. A pattern change is refused while later parts exist.
  - If a later part fails, the error names the parts that were already changed.
- Not changed: `calendar_event_update` and `calendar_event_delete` still change only one
  event or occurrence. A series edit counts as one write call against the per-token write
  limit. Audit lines redact every argument.

Out of scope (from #26): subscribed calendars (already read-only), undo, and splitting a
series into separately named events. This change also leaves out:

- deleting a whole series;
- turning a series into a single event;
- series with `RDATE`, `EXRULE`, several `RRULE`s or `RANGE=THISANDFUTURE` overrides (refused
  with a tool error);
- moving a series to another calendar;
- changing a pattern across several parts in one call.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `calendar-write-tools`: adds the `calendar_series_update` tool. Its requirements cover the
  modes and effective date, matching exceptions to the new pattern, `drop_unmatched`, counts,
  preview, the Apple-format split with its rollback, and split series (recurrence sets). The
  tool set, the people checks and the description rules are extended to the new tool.
- `read-only-and-egress`: "Calendar write DAV methods" lets a series edit send one `PUT` with
  `If-Match` to each part it changes (at most 10). For a split it may also send one `PUT`
  with `If-None-Match: *` of a new resource and, if the cut is refused, one `DELETE` with
  `If-Match` of that resource. Nothing else changes.
- `tool-conventions`: for a token with `calendar_write`, the calendar write tool list and the
  server instructions include `calendar_series_update` (`destructiveHint` true), and the
  instructions say that ids change after a split.

## Impact

- Code:
  - `src/pv_icloud/cal/compose.py`: series edits, the new definition, the Apple-format cut,
    matching exceptions to the new pattern, recurrence sets, counting.
  - `cal/caldav.py`: one new read, the parts of a recurrence set, through `REPORT`.
  - `cal/service.py`: `update_series`, the write order, the rollback and part-by-part writes.
  - `server.py`: the tool, its description and the instructions.
  - Reused as they are: `cal/caldav_write.py` (its methods and preconditions) and
    `cal/expand.py`.
  - Not changed: `dav.py`, `config.py`, `auth.py`, `contacts/` and `mail/`.
- Tests:
  - `tests/fakecaldav.py` gains a hook to fail or change a resource between writes, and a
    `calendar-query` filter on `RELATED-TO`.
  - The tests cover all four modes on a series with deleted and changed occurrences;
    occurrences before the date staying untouched; Apple's `UNTIL` and `RELATED-TO`;
    unmatched exceptions and `drop_unmatched`; counts; preview; rollback; split series
    (field edits, refusals, partial failure, the 10-part cap); and that no scheduling message
    is recorded.
- Docs: ADR-0007 (a prerequisite, written by the maintainer), docs/design.md (the calendar
  write and recurrence sections), README.md (tool list), CHANGELOG.md.
- Egress: the same hosts and methods as before.
- Security posture: one call can now change every future occurrence of a series, across up
  to 10 resources, and discard its exceptions. Prompt injection could cause that. The
  existing mitigations apply: the switch, the grant, protected calendars, the rate limit,
  conditional writes and the "untrusted content is never a reason" text. This change adds
  `preview`, `drop_unmatched` for changed occurrences, the part cap, and a description that
  tells clients to confirm a series edit with the user.
- Compatibility: no breaking change; tokens without `calendar_write` see nothing new.
