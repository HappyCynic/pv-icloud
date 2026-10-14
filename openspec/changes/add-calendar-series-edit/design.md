# Design

## Context

See proposal.md for motivation. Calendar writes exist since #24 under ADR-0005:
`calendar_event_create`, `calendar_event_update` and `calendar_event_delete`, behind
`PV_ICLOUD_CALENDAR_WRITE=enabled` and a token's `calendar_write: true`. `cal/caldav_write.py`
is the only code that sends `PUT` or `DELETE`, one event resource per request, always
conditional and with `Schedule-Reply: F`. `cal/compose.py` builds and edits iCalendar data:
`find_target` locates the occurrence an id names (override, single event, or a local
expansion with `cal/expand.py`), `check_people` refuses resources involving anyone but the
account, and `apply_update`/`apply_delete` change one occurrence by adding or editing an
override (`RECURRENCE-ID`) or an `EXDATE`. `CalendarService._fresh` re-reads the resource
with a single-href `calendar-multiget` and its ETag before every update or delete.

What exists in a stored iCloud series: one master `VEVENT` with `DTSTART` (with `TZID`, or a
date for all-day), usually one `RRULE` (`COUNT` or `UNTIL` or neither), `EXDATE`s for deleted
occurrences, and override `VEVENT`s with the same `UID` and a `RECURRENCE-ID` for changed or
moved occurrences, all in one `.ics` resource.

What Apple Calendar does with "All Future Events" (maintainer's live check on iCloud,
2026-10-14, issue #26): it splits the series. The part from the edited occurrence on becomes
a new resource with a new `UID`; the old master's `RRULE` gets `UNTIL` = 23:59:59 local time
on the day before the edited occurrence, written in UTC (for example `20261103T055959Z` for
America/Chicago and 2026-11-03). Both parts carry
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:<UUID>`, the same random UUID in both,
unrelated to either `UID`. Apple treats every resource with the same value as one series:
"All Future Events" from the first occurrence changed both parts. The old part's earlier
exceptions stay as stored; the new part copies the master's properties (`LOCATION`,
`X-APPLE-STRUCTURED-LOCATION`, `TRANSP`). Apple did not bump `SEQUENCE` on the cut.

Read-only checks of real iCloud data (maintainer, 2026-10-14, issue #26):

- 28 split series: 21 with 2 parts, 3 with 3, none with more. None uses `COUNT` or floating
  times.
- All-day split masters (2 of 2) have `UNTIL` as a `DATE`, the day before the split.
- 17 overrides in real split series carry the recurrence-set `RELATED-TO`, but Apple did not
  add it to existing overrides in the test split.
- iCloud does not support property filters in `calendar-query`: a `prop-filter` on
  `RELATED-TO` (and, as a control, on `SUMMARY`) returns 207 with no matches; a
  `collation="i;octet"` returns 412; a `param-filter` returns 500; a request for partial
  `calendar-data` is ignored and full events come back.
- An open-ended time-range listing from today is 0.5 MB for the largest of 6 calendars; one
  from a year ago is 4.9 MB.

ADR-0005 decision 3 limits every call to one event and leaves "whole-series edit modes" out
of scope; `calendar_event_update`'s description says it never changes a whole series. The
maintainer has accepted the approach below and lands ADR-0007 before implementation; see
decision 11. CLAUDE.md's method list for calendar writes does not change: every request is
still one conditional `PUT` or `DELETE` of one event resource.

Modules touched: `cal/compose` (series edits, splits, recurrence sets), `cal/caldav` (one new
read: an unexpanded time-range listing of a calendar, to find the parts of a recurrence set),
`cal/service` (`update_series`), `server` (the tool and instructions). `cal/caldav_write` and
`cal/expand` are reused unchanged. No new paging: the result is a single object; a recurrence
set is capped at 10 parts, the search for earlier parts reaches back 366 days (decision 6),
and `unmatched` is bounded by the stored resources (decision 8).

## Goals / Non-Goals

**Goals:**

- The four modes of #26, on series with deleted and changed occurrences.
- Occurrences before the effective date, and their exceptions, stay exactly as stored in the
  "from" modes.
- No exception disappears without being reported, and no changed occurrence is dropped
  unless the caller says so.
- Store splits the way Apple Calendar does, so a split series is one series in Apple's apps,
  and treat a series Apple has already split as one series.
- The same safety envelope as the existing calendar writes: switch, grant, writable and
  unprotected calendars only, conditional writes after a fresh read, no attendees and no
  email to anyone, shared write limit, redacted audit.
- The usual failure (the series changed in Calendar meanwhile) leaves the calendar as it was
  when it happens on the split; a later failure says exactly what changed.

**Non-Goals:**

- Undo, subscribed calendars, splitting into separately named events (#26 out of scope).
- Deleting a whole series, or turning a series into a single event.
- `RDATE`, `EXRULE`, several `RRULE`s, `RANGE=THISANDFUTURE` overrides: refused.
- Recurrence rules beyond what `calendar_event_create` writes (`BYMONTHDAY`, `BYSETPOS` and
  so on). An existing rule with those parts is kept when `recurrence` is not given.
- Moving a series to another calendar; parts of a recurrence set in another calendar are not
  looked for.
- Changing the pattern of a series across several parts in one call (decision 7).

## Decisions

### 1. One tool, `calendar_series_update`, with a `mode`

```
calendar_series_update(
  id,                       # any occurrence of the series, as the read tools return it
  mode,                     # replace_all | replace_from | update_all | update_from
  effective=None,           # ISO date; required for *_from, refused for *_all
  title, start, end, all_day, timezone, location, notes, alerts, clear,   # as calendar_event_update
  recurrence=None,          # as calendar_event_create
  drop_unmatched=False,     # update_*: allow dropping changed occurrences that no longer fit
  preview=False,
)
```

One tool with a `mode` enum maps one to one to the issue's four modes and keeps the
whole-series path visibly apart from `calendar_event_update`, whose "one event or
occurrence" promise stays as it is. Alternatives considered: two orthogonal arguments
(`scope: all|from`, `exceptions: keep|discard`), equivalent but less obvious to read in a
transcript; or new modes on `calendar_event_update`, which would blur its guarantee and its
annotations. `destructiveHint` is true.

The id may name any occurrence of the series, the master's or an override's, in any part of
a recurrence set (decision 7). A non-recurring event fails with `not a recurring series; use
calendar_event_update`. A series with `RDATE`, `EXRULE`, more than one `RRULE`, an override
with `RANGE=THISANDFUTURE`, or a lone override without its master fails with `this series
cannot be changed here; change it in Calendar`, before any write. The same applies to every
part of a recurrence set.

### 2. The new definition

Fields not given keep the series' current values in every mode; the modes differ only in
what happens to exceptions and to occurrences before the date. `title`, `location`,
`notes`, `alerts`, `clear`, `timezone` and `all_day` mean what they mean for
`calendar_event_update`, applied to the master (the series definition).

`start`/`end` set the times of the series' first changed occurrence: the series' first
occurrence in the `_all` modes, the first occurrence on or after `effective` in the `_from`
modes (whose date must then not be before `effective`). A new `start` without `end` keeps
the length; changing only `timezone` keeps the wall-clock times, as for single events.

`recurrence` replaces the rule entirely (same object and limits as `calendar_event_create`;
its `count` or `until` bound the changed part). Without it the current rule is kept (cut at
the date in the `_from` modes, decision 4).

Where the changed series begins: from an anchor, which is `start` if given, otherwise the
current first occurrence (`_all`) or the first original occurrence on or after `effective`
(`_from`), the new `DTSTART` is the first occurrence the new rule produces on or after the
anchor, at the new time of day. This avoids RFC 5545's rule that `DTSTART` is always an
occurrence, which would otherwise add a stray occurrence when the anchor's weekday is not in
the new `BYDAY`. A definition that yields no occurrence is a tool error.

`start`, `end`, `recurrence`, `all_day` and `timezone` are the **pattern fields**; `title`,
`location`, `notes`, `alerts` and `clear` of those are **field edits**. The distinction
matters only for recurrence sets with several parts (decision 7).

`update_all` and `update_from` with nothing to change are a tool error (`nothing to
change`). `replace_all` or `replace_from` with no field is allowed: it resets the series by
discarding its exceptions (from the date).

### 3. Exceptions and which side of the date they are on

An exception is an `EXDATE` value (kind `deleted`) or an override (kind `changed`; an
override with `STATUS:CANCELLED` counts as `deleted`). Its original start is the `EXDATE`
value or the override's `RECURRENCE-ID`, and that, not where a changed occurrence was moved
to, decides which side of `effective` it is on. The day of a start is taken in the series'
zone (`DTSTART`'s `TZID`; the account's default zone for all-day or floating series).

- `replace_*`: exceptions in range (all, or on or after the date) are discarded and counted.
- `update_*`: each exception in range is matched to the new series (decision 5).

### 4. Storing "from a date": split into two resources, as Apple does

There is no single-resource way to give part of a series a different rule: one `UID` has one
master, and `RANGE=THISANDFUTURE` overrides cannot change a pattern, are not written by
Apple's apps, and are not supported by `cal/expand.py`. So the `_from` modes split the part
of the series that contains the date, in Apple's format (Context):

- **The existing resource is cut.** The master's rule gets `UNTIL` = 23:59:59 local time on
  the day before `effective`, in the series' zone, written in UTC (for example
  `UNTIL=20261103T055959Z` for an America/Chicago series and `effective` 2026-11-03). A
  `COUNT` is replaced by that `UNTIL` (RFC 5545 forbids both; the occurrences before the date
  are the same either way, since `EXDATE`s do not change what `COUNT` counts). For an all-day
  series the `UNTIL` is a `DATE`, the day before `effective` (Apple's form in real data). For a
  floating series it is that local time without a zone, as RFC 5545 requires the value type
  of `DTSTART` (no real split series uses `COUNT` or floating times; the maintainer accepted
  both forms). `EXDATE` values and overrides on
  or after the date are removed from it. Everything else, earlier overrides, `VTIMEZONE`s and
  `X-APPLE-*` properties included, is kept as stored; the master gets a higher `SEQUENCE`
  (Apple does not bump it; the maintainer accepted bumping) and a new `LAST-MODIFIED`.
- **The rest becomes a new series** in a new resource in the same calendar: a copy of the
  master with all its properties and alarms (`LOCATION`, `X-APPLE-STRUCTURED-LOCATION`,
  `TRANSP` and every other one, as Apple does) and the new definition applied, a new random
  UUID as `UID` and resource name, `SEQUENCE` 0, new `CREATED`/`DTSTAMP`/`LAST-MODIFIED`,
  no `ORGANIZER` or `ATTENDEE`, the matching `VTIMEZONE`s, the new rule (or the rest of the
  old one: the original `UNTIL`, or the original `COUNT` minus the occurrences before the
  date), and in `update_from` the matched exceptions, re-keyed (decision 5) and given the new
  `UID`.
- **Both parts carry the recurrence set.** If the old master has
  `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET`, its value is reused; otherwise a new
  random UUID, unrelated to either `UID`, is generated and added to the old master. The new
  master gets the same value.
- **Overrides follow Apple's real data.** Every override pv-icloud creates or re-keys (the
  kept overrides in the new resource, and overrides re-keyed in place in an `update_*` mode,
  decision 5) carries the master's recurrence-set value, the way a new override is a copy of
  the master: the property is set to the master's value, or removed when the master has
  none. Overrides pv-icloud does not create or re-key, such as the cut part's earlier ones,
  are left as stored.
- `effective` must have at least one original occurrence on or after it in the set;
  otherwise the call fails (`the series has no occurrence on or after <date>`). When
  `effective` is on or before the first occurrence of the part that would be cut (the part
  found first from it, decision 6), there is nothing to cut: that part is changed in place,
  with or without a recurrence-set value, and the result says that no split was needed
  (`parts.split: false`; maintainer's answer on #26).
- **Occurrence ids from the date change**: an id names the calendar, the resource and the
  start, and the occurrences on or after the date now live in the new resource. The description says so and the result
  carries `ids_changed_from` (decision 8).

The `_all` modes change the one resource in place, with one `PUT` with `If-Match`.

### 5. Matching exceptions to the new pattern (`update_*`)

An exception in range matches when the new series has exactly one occurrence on the same day
as the exception's original start. It is then re-keyed to that occurrence: an `EXDATE`
becomes that occurrence's start, and an override's `RECURRENCE-ID` becomes it (same value
type and `TZID` as the new `DTSTART`), and a re-keyed override takes the master's
recurrence-set value (decision 4). With an unchanged pattern and time every exception
matches as it is; with only a new time of day every exception matches and is re-keyed; with
a new weekday set, exceptions on days that drop out do not match.

A matched override keeps what was changed on it. For each field the call changes (`title`,
`location`, `notes`, `alerts`), the override takes the new value only when its own value was
the master's (both unset counts as equal); otherwise it keeps its own. This is what Apple's
apps do (maintainer's live check: overrides without their own notes took the new notes; a
retitled one kept its title). For times: an override that was not moved (`DTSTART` equals its
original start and its length is the series' length) takes its matched occurrence's new
times; a moved one keeps its own start and end. Changing `all_day` leaves no override matched
(their times cannot be carried over); `EXDATE`s still match by day.

An exception that does not match (no occurrence that day, or more than one) cannot be kept:
it would turn into an orphan override or a no-op `EXDATE`. It is always listed in the
result's `unmatched` (preview included): `kind`, `original_start`, and for a `changed` one
its current `start`, `end` and `title`, so the user can recreate it with
`calendar_event_update` or `calendar_event_create`.

- An unmatched **deleted** occurrence is dropped and listed.
- An unmatched **changed** occurrence makes the call fail, before any write, with `<n>
  changed occurrences no longer fit the new pattern; preview to see them, or pass
  drop_unmatched: true to drop them`, unless `drop_unmatched` is true; then it is dropped and
  listed. The error carries only the count, not the occurrences' content.
- A preview never fails for this reason: it returns the result with `unmatched` filled and
  `requires_drop_unmatched: true` when the call would need `drop_unmatched`.

`drop_unmatched` is ignored by `replace_*`, which discard every exception in range by
definition.

### 6. Finding the recurrence set

The series' own resource (the *named part*, the one the id points into) is read first, as
today, with a single-href `calendar-multiget`. A series whose master has no
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` is a set of one part, and nothing more
is read.

Otherwise the other parts are found by listing the series' calendar by time, because iCloud
does not support property filters (Context). Each listing is one `calendar-query` `REPORT`
for `VEVENT`s with a `time-range`, asking for `getetag` and unexpanded `calendar-data`; the
server then keeps, locally, the resources whose master carries
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` with exactly the named part's value.
The listings never carry a `prop-filter`, a `param-filter` or a `collation` (iCloud answers
them wrongly, with 207 and no matches, 412 or 500). Two windows:

- **Later parts, every mode:** `<c:time-range start="…"/>` with no end, starting at the
  named part's last occurrence in the `_all` modes, or at `effective` in the `_from` modes. A
  time-range matches a recurring resource when any of its occurrences falls in it, so in the
  `_from` modes this also finds the part that contains `effective` when the id names an
  earlier part. In the `_all` modes, when the named part has no last occurrence (it is
  endless, so no part can follow it), this listing is skipped.
- **Earlier parts, `_all` modes only:** a range from 366 days before today to the named
  part's first occurrence, skipped when that occurrence is before the range's start. A part
  whose occurrences all end before the window is not found and not changed. The result gives
  the window's start as `parts.searched_from`, and the tool description says that parts of a
  series that ended more than a year ago are not changed.

The maintainer's measurements put an open-ended listing from today at 0.5 MB for the largest
of six calendars and a listing from a year back at 4.9 MB; the earlier-parts listing runs
only for `_all` modes on a series that carries a recurrence set, and the `_from` modes never
look back. Both are `REPORT`s through the read-only DAV base; `cal/caldav.py` gets one new
read method for them (an unexpanded variant of `events`, with an optional end). Parts in
other calendars are not looked for.

The `getetag` and data from a listing are that part's fresh read (a read of that resource in
the same call, as ADR-0005 requires for `If-Match`); no further request reads it. A set of
more than **10 parts** found is refused, before any write, with `this series has more than
10 parts; change it in Calendar`. Parts are ordered by their first occurrence, and the
people, writable-calendar and series-shape checks run on every part found before any write.

### 7. Recurrence sets with several parts

Apple treats every part of a set as one series, so a series edit does too, within these
limits (from the maintainer's answer on #26):

- **`update_all` with only field edits** applies to every part: each part gets one `PUT` with
  `If-Match` from its fresh read, with the field edits applied to its master and propagated
  to its overrides as in decision 5. The parts' patterns are unchanged, so every exception is
  kept.
- **`replace_all`, and `update_all` that changes a pattern field**, on a set of more than one
  part are refused before any write: `this series was split; change it from a date
  (replace_from / update_from)`.
- **`_from` modes** change the part that contains `effective` as in decision 4 (split it, or
  change it in place when `effective` is on or before its first occurrence), and treat every later part like `update_all`: field edits are
  applied to it, each with its own `PUT` and `If-Match`. When there are later parts, a
  pattern field is refused (`this series was split on <date>; change its pattern from that
  date or later`), and so is `replace_from` (confirmed by the maintainer), because a later
  part's own pattern and exceptions cannot be reconciled with a new one in one call.
- Parts before the one containing `effective` are not looked for and not changed.
- In the `_all` modes, the refusals above apply when more than one part is found by the two
  listings of decision 6; parts that ended before the earlier-parts window are not found, so
  they neither cause a refusal nor are changed.

Writes go part by part in order of first occurrence, the split (decision 9) first. If a
later part's `PUT` fails, nothing more is sent and the call fails with an error that names
the parts already changed and the part that failed (by `uid` and first occurrence), so the
user can finish in Calendar or call again. Nothing is retried or undone, apart from the
split's rollback.

### 8. The result

```
{
  "mode": "update_from",
  "preview": false,
  "id": "...", "uid": "...", "calendar": "...", "calendar_id": "...",   # first changed occurrence
  "start": "...", "end": "...", "timezone": "...", "all_day": false, "recurring": true,
  "occurrences": 142,                  # occurrences that now follow the new definition
  "occurrences_through": null,         # set when counting stopped (endless series)
  "exceptions": {"kept": 9, "discarded": 0, "unmatched": 2},
  "unmatched": [{"kind": "changed", "original_start": "...", "start": "...", "end": "...",
                 "title": "..."}, {"kind": "deleted", "original_start": "..."}],
  "requires_drop_unmatched": false,    # preview only
  "parts": {"found": 2, "changed": 2, "split": true, "searched_from": "2025-10-14"},
  "previous": {"uid": "...", "last_start": "..."},                      # split only
  "ids_changed_from": "2027-01-11"                                       # split only
}
```

`id` and the times are those of the first occurrence of the changed part, formatted by the
read path's own code (as the existing tools do), and readable with `calendar_event` at once.
`occurrences` counts the changed occurrences (in every changed part) after deleted ones are
removed, up to the last one, or, for an endless series, through 366 days after the first
changed occurrence, in which case `occurrences_through` is that last day counted.
`exceptions` counts over every changed part. `unmatched` is not capped: its length is bounded
by the exceptions stored in at most 10 resources, and its count is also in
`exceptions.unmatched`. `parts` gives how many parts of the set were found (1 for a series
without a recurrence set), how many the call changes, `split` (whether a part was split
into two resources; false in the `_all` modes and when a `_from` mode changed a part in
place), and `searched_from`: in the `_all`
modes for a series with a recurrence set, the start of the earlier-parts window (decision 6;
parts that ended before it were not looked for), even when that listing was skipped;
otherwise null (in the `_from` modes nothing before `effective` is looked for).
After a split, `ids_changed_from` is the effective date: occurrence ids on or after it have
changed and must be read again. Times in the result are in the event's zone, as everywhere
else. Titles in `unmatched` are event content and the result keeps the read tools'
`content_trust: untrusted` marking.

### 9. Order of writes and rollback

1. Fresh reads: the series' resource with a single-href `calendar-multiget` (data and ETag),
   then, for a series with a recurrence set, the time-range listings of decision 6 (data and
   ETag of every other part); writable-calendar, people and
   series-shape checks on every part; part-count cap; unmatched check (decision 5); every
   resource computed. Nothing is sent if any check fails.
2. With `preview`, return the result here.
3. `_all` modes: one `PUT` with `If-Match` per part, in order (decision 7). Done.
4. `_from` with a split: `PUT` of the new resource with `If-None-Match: *`. If it fails,
   nothing changed (or, when no answer arrived, the error says so as today).
5. `PUT` of the cut part with `If-Match` = the ETag from step 1.
6. If step 5 is answered with 412, 404 or another 4xx (iCloud made no change to the old
   resource), the new resource is read once (single-href `calendar-multiget`) and deleted
   with `DELETE` and `If-Match` = that ETag, then the call fails with step 5's error (for
   412: `the event changed in Calendar; read it again`). The calendar is as it was.
7. If step 5 gets no answer, is throttled, or the rollback in step 6 fails, nothing more is
   sent and the call fails with an error saying that the series from `<effective>` may now
   be stored twice, naming the new series' `id`, and that the user should check it in
   Calendar.
8. `_from` without a split (`effective` at a later part's start): one `PUT` with `If-Match`
   of that part instead of steps 4 to 7.
9. Later parts: one `PUT` with `If-Match` each, in order; a failure stops the call as in
   decision 7.

No step is retried. A call sends at most one write request per part plus, for a split, one
for the new resource and one rollback `DELETE`; with the 10-part cap, at most 12.

Creating first means the likeliest failure, a conflict on the existing resource, can be
undone by removing a resource this call itself created a moment ago. Cutting first would mean
a failed create loses every future occurrence. The rollback `DELETE` meets ADR-0005's rules
(one event resource, `If-Match` from a read in the same call, `Schedule-Reply: F`). Between
steps 4 and 5 another client could briefly see the occurrences from the date twice.

### 10. Preview

`preview: true` runs steps 1 and 2: it reads, checks and computes everything and returns the
same result with `preview: true`, sending no write. It exists because a series edit can
discard many exceptions and leave some unmatched; the description tells clients to preview
and confirm with the user when exceptions would be discarded or unmatched. A preview is still
a call to a write tool, so it counts against the token's hourly write limit like any other
call (confirmed by the maintainer).

### 11. ADR-0007 (prerequisite)

The maintainer has accepted the two-resource split with the rollback `DELETE` and lands
ADR-0007 before implementation, covering recurrence sets as well. What this design assumes
it decides:

1. A calendar write call may change one whole series, or one series from a date on, through
   `calendar_series_update`; `calendar_event_update` and `calendar_event_delete` keep "one
   event or occurrence per call". This amends ADR-0005 decision 3.
2. A series edit "from a date" may send, in one call, a `PUT` with `If-None-Match: *` of one
   new event resource and a `PUT` with `If-Match` of the series' resource, plus, only to undo
   the first after the second was refused, a `DELETE` with `If-Match` of that new resource.
   Splits are stored in Apple's format (`UNTIL` and
   `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET`, on both masters and on every override
   pv-icloud creates or re-keys).
3. Resources sharing a recurrence set are one series: a series edit may send one `PUT` with
   `If-Match` to each of up to 10 parts in the same calendar, field edits only across parts,
   no rollback of earlier parts on a later failure, which is reported part by part. Parts are
   found with time-range `calendar-query` listings of that calendar (open-ended from the
   named part or `effective`; back to 366 days before today for earlier parts in the `_all`
   modes), matched locally, never with a `prop-filter`, `param-filter` or `collation`.
4. Everything else in ADR-0005 applies unchanged: one event resource per request, its method
   list and preconditions, writable and unprotected calendars, no attendees and no events
   involving anyone else, `Schedule-Reply: F`, 412 reported and never retried, one
   write-limit budget (a series edit counts once, a preview included), redacted audit.

Task 1.1 checks the merged ADR against this list before any code is written.

### 12. Audit, limits, instructions

Audit lines are as for every calendar write: `write: true`, every argument redacted
(`mode`, `effective`, `drop_unmatched` and `preview` included), `result_count` 1. Field
limits are those of the existing tools; `effective` must be an ISO date; `mode` one of the
four values; `drop_unmatched` and `preview` booleans. The rate-limit check runs first, as for
every write tool. The instructions for a token with `calendar_write` name the new tool and
say it changes a whole series or a series from a date, that it can discard deleted and
changed occurrences, that it should be previewed and confirmed with the user, and that
occurrence ids from a split date change. The tool description also says that, on a series
split in Calendar, parts that ended more than a year ago are not found or changed.

## Risks / Trade-offs

- **One call changes many occurrences, and up to 10 resources** → `preview`, description and
  instructions that ask for confirmation, `drop_unmatched` for changed occurrences, protected
  calendars, the switch and grant, the part cap, and a destructive annotation.
- **Split series** → stored as Apple stores them, so Apple's apps show one series; other
  clients may show two events. The previous part keeps its `UID`, so links to past
  occurrences still work; ids from the date change and the result says so.
- **Brief duplicate between the two writes, or a lasting one if the second gets no answer**
  → ordered so the likely failure is rolled back; the unlikely one is reported with the new
  series' id, never retried.
- **Partial failure across parts** → only field edits are applied across parts, so a
  half-applied call leaves each part a valid series; the error names the parts changed.
- **Finding parts needs calendar listings** because iCloud has no property filter (up to
  about 5 MB for a year-back listing of a large calendar) → only for series that carry a
  recurrence set; the `_from` modes and the later-parts search only list forward from a
  date; the year-back listing runs only in the `_all` modes.
- **Parts that ended more than a year ago are not found** by `update_all` → they are left as
  they are, which is harmless for field edits; the result's `parts.searched_from` and the
  description say so.
- **Rules beyond `calendar_event_create`'s subset** → kept when `recurrence` is not given,
  replaced when it is; matching uses the rule as stored via `cal/expand.py`.

## Migration Plan

None: a new tool for tokens that already have `calendar_write`. Rollback is removing the
tool; stored series written by it are ordinary iCalendar data in Apple's format.

## Open Questions

Answered on #26 (2026-10-14): ADR-0007 and the split are accepted; Apple's split format,
including all-day `UNTIL` and the recurrence set on overrides; preview counts against the
write limit; unmatched changed occurrences need `drop_unmatched`; the override propagation
rule; recurrence sets, the 10-part cap, `replace_from` refused with later parts, a later part
changed in place; finding parts by time-range listings without property filters; the
year-back listing in both `_all` modes (real calendars hold recurrence sets with a single
remaining part, which refusing `replace_all` on any recurrence-set value would block); and
`effective` on or before the first occurrence of the part that would be cut changing that
part in place, with or without a recurrence-set value (`parts.split: false`). None remain.
