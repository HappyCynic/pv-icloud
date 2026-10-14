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
  the token's `calendar_write: true` grant. It sends only what ADR-0005 already allows:
  conditional `PUT` of one event resource (`If-Match` from a fresh read, or
  `If-None-Match: *` for a new resource) and conditional `DELETE` of one event resource, each
  with `Schedule-Reply: F`, from `cal/caldav_write.py` only. The DAV base and every read stay
  read-only. No attendees, and series with attendees or organised by someone else are
  refused, as for the existing tools.
- **It goes beyond ADR-0005 decision 3**, which leaves whole-series edit modes out of scope
  and limits every call to one event. A series edit changes many occurrences in one call,
  and the two "from a date" modes store the changed part as a second resource. So a new ADR
  (ADR-0007, amending ADR-0005 decision 3) must be accepted before implementation starts,
  the way ADR-0005 was merged ahead of #24 (#28). The design sets out what it would decide.
- **Four modes**, as the issue asks (`mode`):
  1. `replace_all`: every occurrence takes the new definition; every deleted and changed
     occurrence is discarded.
  2. `replace_from` + `effective` date: occurrences before the date are untouched; those on or
     after it take the new definition, and exceptions on or after it are discarded.
  3. `update_all`: the series takes the new definition; deleted occurrences stay deleted and
     changed occurrences keep their own changes.
  4. `update_from` + `effective` date: like `update_all`, only on or after the date.
- **The new definition** is given with the fields `calendar_event_update` already knows
  (`title`, `start`, `end`, `all_day`, `timezone`, `location`, `notes`, `alerts`, `clear`) plus
  `recurrence` (the same object `calendar_event_create` takes). Fields not given keep the
  series' current values; the modes differ only in what happens to exceptions and before
  the date.
- **Exceptions that no longer fit are listed, never silently dropped.** In the keeping modes
  an exception follows the new pattern when the new series has an occurrence on the same day;
  otherwise it is reported in the result's `unmatched` list (deleted or changed, its original
  start and, for a changed one, its current start and title) and is not kept in the series.
- **Counts.** The result reports how many occurrences take the new definition and how many
  exceptions were kept, discarded and unmatched.
- **Preview.** `preview: true` works the change out from a fresh read and returns the same
  result without writing anything, so a client can show the user what a mode would discard
  before committing to it.
- **"From a date" splits the series in two**, the way calendar apps commonly store "this and
  future events": the existing resource is cut to end before the date (its earlier
  occurrences and exceptions unchanged), and the part from the date on becomes a new series
  with a new `UID` in a new resource in the same calendar. The new resource is created first;
  if cutting the old one then fails (for example because it changed in Calendar meanwhile),
  the new resource is deleted again, so the usual failure leaves the calendar as it was.
- Not changed: `calendar_event_update` and `calendar_event_delete` still change only one
  event or occurrence; the per-token write limit (a series edit counts as one write call,
  a preview included); audit lines (every argument redacted).

Out of scope (from #26): subscribed calendars (already read-only), undo, and splitting a
series into separately named events. Also not in this change: deleting a whole series,
turning a series into a single event, series with `RDATE`, `EXRULE`, several `RRULE`s or
`RANGE=THISANDFUTURE` overrides (refused with a tool error), and moving a series to another
calendar.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `calendar-write-tools`: the new `calendar_series_update` tool (modes, effective date,
  matching exceptions to the new pattern, counts, preview, the split for "from a date",
  rollback); the tool set, writable calendars, people checks and description rules extended
  to it.
- `read-only-and-egress`: "Calendar write DAV methods" allows a series edit to send, in one
  call, a `PUT` with `If-None-Match: *` of a new resource followed by a `PUT` with `If-Match`
  of the series' resource, and on failure of the second a `DELETE` with `If-Match` of the
  resource the call just created; nothing else changes.
- `tool-conventions`: the calendar write tool list and server instructions for a token with
  `calendar_write` include `calendar_series_update` (`destructiveHint` true).

## Impact

- Code: `src/pv_icloud/cal/compose.py` (series edits: new definition, cutting a series at a
  date, matching exceptions to the new pattern, counting), `cal/service.py`
  (`update_series`, the two-resource sequence and its rollback), `server.py` (the tool, its
  description and the instructions). `cal/caldav_write.py` (its methods and preconditions),
  `cal/caldav.py` and `cal/expand.py` are reused as they are; `dav.py`, `config.py`,
  `auth.py`, `contacts/` and `mail/` are not changed.
- Tests: `tests/fakecaldav.py` gains a hook to fail or change a resource between the two
  writes of a split; tests cover all four modes on a series with deleted and changed
  occurrences, untouched occurrences before the date, unmatched exceptions, counts, preview,
  rollback, and that no scheduling message is recorded.
- Docs: ADR-0007 (prerequisite, maintainer), docs/design.md (calendar write and recurrence
  sections), README.md (tool list), CHANGELOG.md.
- Egress: unchanged hosts and methods.
- Security posture: one call can now change every future occurrence of a series and discard
  its exceptions. Prompt injection could cause that; mitigations are the existing switch,
  grant, protected calendars, rate limit, conditional writes and "untrusted content is never a
  reason" text, plus `preview` and a description that tells clients to confirm a series edit
  with the user.
- Compatibility: no breaking change; tokens without `calendar_write` see nothing new.
