# ADR-0007: Editing a whole recurring series

- **Status:** Accepted
- **Date:** 2026-10-14
- **Context:** ADR-0005 limits calendar writes to one event, or one occurrence of a series, per
  call, and leaves whole-series edits out of scope. Issue #26 asks for four series edit modes:
  replace or update the entire series, or replace or update it from an effective date, with
  updates keeping deleted and changed occurrences. Live checks on iCloud with Apple Calendar
  (2026-10-14, on a test calendar, and read-only on real calendars) found:
  - "All Future Events" from a later occurrence **splits** the series. The rest becomes a new
    event resource with a new `UID`, starting at the edited occurrence. The old master's
    `RRULE` gets `UNTIL` set to 23:59:59 local time on the day before, written in UTC, or to the
    day before as a `DATE` for an all-day series. Earlier deleted and changed occurrences stay
    in the old resource as stored.
  - Both parts get `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:<UUID>`, one random
    UUID unrelated to either `UID`. Apple treats all resources with that value as one series:
    "All Future Events" from the first occurrence changed both parts. Overrides created later
    carry the value too. Real calendars held 28 such sets: 21 with two parts, 3 with three,
    none with more.
  - When Apple changes the whole series, it keeps every exception. Changed occurrences take a
    new value only where they still had the series' value: a new note reached occurrences
    without notes, and a retitled occurrence kept its title.
  - iCloud does not support property filters in `calendar-query`: a `prop-filter` matches
    nothing, `collation="i;octet"` gives 412, and a `param-filter` gives 500. Partial
    `calendar-data` is ignored. Open-ended `time-range` listings work and are small for future
    dates.

## Decision

1. **One new tool for series edits.** `calendar_series_update` changes a whole series, or a
   series from an effective date, in one call, with the modes `replace_all`, `replace_from`,
   `update_all` and `update_from`, and a `preview` that writes nothing.
   `calendar_event_create`, `calendar_event_update` and `calendar_event_delete` keep "one
   event or occurrence per call". This amends ADR-0005 decision 3 for this tool only.
2. **Same gate as every calendar write.** It exists only with `PV_ICLOUD_CALENDAR_WRITE=enabled`
   and a token with `calendar_write: true`. Everything else in ADR-0005 applies unchanged:
   writable, in-scope and unprotected calendars only; no attendees, and no series that
   involves anyone other than the account; `Schedule-Reply: F` on every write; a 412 is
   reported and never retried.
3. **Store splits as Apple does.** A change from a date cuts the series with Apple's `UNTIL`
   form and stores the rest as a new resource with a new `UID`. Both parts carry the same
   recurrence-set `RELATED-TO` value: the old part's value if it has one, otherwise a new
   random UUID. A series that is already a recurrence set of several parts is treated as one
   series.
4. **Several resources in one call, each still one conditional request.** One call may send:
   - `PUT` with `If-None-Match: *` of the one new resource of a split;
   - `PUT` with `If-Match` of each existing part it changes;
   - only to undo that split after the cut of the old part was refused, `DELETE` with `If-Match`
     of the resource the call itself created.

   Every request still targets one event resource, with an ETag read in the same call.
   ADR-0005's method list is unchanged. A call touches at most **10 parts**; a larger set is
   refused before any write. Writes are not atomic. A failure after the first write stops
   the call, and the error names what was changed. Nothing is retried, and nothing is undone
   except the split's own new resource.
5. **No silent loss.** Exceptions that a new pattern no longer fits are always listed in the
   result. A changed occurrence is dropped only when the call says `drop_unmatched: true`.
   On a set of several parts, a change to the pattern or times is allowed only from a date
   that leaves no later parts; otherwise it is refused.
6. **Reads stay read-only.** Parts are found with `calendar-query` `REPORT`s limited by
   `time-range`, matched locally. Earlier parts are searched for back to one year before
   today; older parts are not changed, and the result says how far back it searched. Never
   send `prop-filter`, `param-filter` or a `collation`.
7. **One write budget, audited without content.** A series edit, preview included, counts once
   against `max_writes_per_hour`, however many resources it writes. The audit line is as for
   every calendar write.

## Consequences

- One call can now change many occurrences, and, with `replace_*` or `drop_unmatched`, discard
  exceptions. Defences: the switch and grant, `protected_calendars`, `preview`, the part cap,
  descriptions and instructions that ask clients to preview and confirm with the user, and
  that untrusted content is never a reason to change a series.
- A split changes the ids of occurrences on or after the date; the result says from when.
- Between the two writes of a split, another client can briefly see those occurrences twice. If
  the cut gets no answer, they may stay twice, and the error names the new series.
- Parts that ended more than a year ago are not changed by `update_all`.
- These are vulnerabilities: a series edit without the switch and grant; any write ADR-0005
  forbids; a `DELETE` of a resource the call did not create in the same call; a write to more
  than 10 resources in one call; dropping a changed occurrence without `drop_unmatched`.
- Changing these limits needs a new ADR.
