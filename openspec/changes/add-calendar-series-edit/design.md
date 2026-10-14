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

ADR-0005 decision 3 limits every call to one event and leaves "whole-series edit modes" out
of scope; `calendar_event_update`'s description says it never changes a whole series. This
change needs a new ADR (ADR-0007) to amend that decision; see decision 9. CLAUDE.md's method
list for calendar writes does not change.

Modules touched: `cal/compose` (series edits), `cal/service` (`update_series`), `server`
(the tool and instructions). `cal/caldav_write`, `cal/caldav` and `cal/expand` are reused
unchanged. No new paging: the result is a single object; the `unmatched` list is bounded by
the stored resource (see decision 6).

## Goals / Non-Goals

**Goals:**

- The four modes of #26, on series with deleted and changed occurrences.
- Occurrences before the effective date, and their exceptions, stay exactly as stored in the
  "from" modes.
- No exception disappears without being reported.
- The same safety envelope as the existing calendar writes: switch, grant, writable and
  unprotected calendars only, conditional writes after a fresh read, no attendees and no
  email to anyone, shared write limit, redacted audit.
- The usual failure (the series changed in Calendar meanwhile) leaves the calendar as it was.

**Non-Goals:**

- Undo, subscribed calendars, splitting into separately named events (#26 out of scope).
- Deleting a whole series, or turning a series into a single event.
- `RDATE`, `EXRULE`, several `RRULE`s, `RANGE=THISANDFUTURE` overrides: refused.
- Recurrence rules beyond what `calendar_event_create` writes (`BYMONTHDAY`, `BYSETPOS` and
  so on). An existing rule with those parts is kept when `recurrence` is not given.
- Moving a series to another calendar.

## Decisions

### 1. One tool, `calendar_series_update`, with a `mode`

```
calendar_series_update(
  id,                       # any occurrence of the series, as the read tools return it
  mode,                     # replace_all | replace_from | update_all | update_from
  effective=None,           # ISO date; required for *_from, refused for *_all
  title, start, end, all_day, timezone, location, notes, alerts, clear,   # as calendar_event_update
  recurrence=None,          # as calendar_event_create
  preview=False,
)
```

One tool with a `mode` enum maps one to one to the issue's four modes and keeps the
whole-series path visibly apart from `calendar_event_update`, whose "one event or
occurrence" promise stays as it is. Alternatives considered: two orthogonal arguments
(`scope: all|from`, `exceptions: keep|discard`), equivalent but less obvious to read in a
transcript; or new modes on `calendar_event_update`, which would blur its guarantee and its
annotations. `destructiveHint` is true.

The id may name any occurrence of the series, the master's or an override's; the series is
the master with that `UID` in the resource. A non-recurring event fails with `not a recurring
series; use calendar_event_update`. A series with `RDATE`, `EXRULE`, more than one `RRULE`, an
override with `RANGE=THISANDFUTURE`, or a lone override without its master fails with `this
series cannot be changed here; change it in Calendar`, before any write.

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

### 4. Storing "from a date": split into two resources

There is no single-resource way to give part of a series a different rule: one `UID` has one
master, and `RANGE=THISANDFUTURE` overrides cannot change a pattern, are not written by
Apple's apps, and are not supported by `cal/expand.py`. Writing every future occurrence as
an `RDATE` plus overrides is unbounded for an endless series. So the `_from` modes split the
series, the way calendar apps commonly store "this and future events":

- **The existing resource is cut.** The master's rule ends before the date: `UNTIL` becomes
  the last generated occurrence before `effective` (in UTC for a zoned series, a date for an
  all-day one), or a `COUNT` becomes the number of occurrences generated before it (`EXDATE`s
  do not change what `COUNT` counts, so the earlier occurrences stay the same). `EXDATE`
  values and overrides on or after the date are removed from it. Everything else, earlier
  overrides, `VTIMEZONE`s and `X-APPLE-*` properties included, is kept as stored; the master
  gets a higher `SEQUENCE` and a new `LAST-MODIFIED`.
- **The rest becomes a new series** in a new resource in the same calendar: a copy of the
  master with the new definition, a new random UUID as `UID` and resource name, `SEQUENCE` 0,
  new `CREATED`/`DTSTAMP`/`LAST-MODIFIED`, no `ORGANIZER` or `ATTENDEE`, the new rule (or the
  rest of the old one: the original `UNTIL`, or the original `COUNT` minus the occurrences
  before the date), and in `update_from` the matched exceptions, re-keyed (decision 5) and
  given the new `UID`.
- `effective` must have at least one original occurrence before it and one on or after it;
  otherwise the call fails (`no occurrence before <date>; use replace_all or update_all`, or
  `the series has no occurrence on or after <date>`).

The `_all` modes change the one resource in place, with one `PUT` with `If-Match`.

### 5. Matching exceptions to the new pattern (`update_*`)

An exception in range matches when the new series has exactly one occurrence on the same day
as the exception's original start. It is then re-keyed to that occurrence: an `EXDATE`
becomes that occurrence's start, and an override's `RECURRENCE-ID` becomes it (same value
type and `TZID` as the new `DTSTART`). With an unchanged pattern and time every exception
matches as it is; with only a new time of day every exception matches and is re-keyed; with
a new weekday set, exceptions on days that drop out do not match.

A matched override keeps what was changed on it. For each field the call changes (`title`,
`location`, `notes`, `alerts`), the override takes the new value only when its own value was
the master's (both unset counts as equal); otherwise it keeps its own. For times: an override
that was not moved (`DTSTART` equals its original start and its length is the series'
length) takes its matched occurrence's new times; a moved one keeps its own start and end.
Changing `all_day` leaves no override matched (their times cannot be carried over); `EXDATE`s
still match by day.

An exception that does not match (no occurrence that day, or more than one) is not kept: it
would otherwise turn into an orphan override or a no-op `EXDATE`. It is listed in the result's
`unmatched`: `kind`, `original_start`, and for a `changed` one its current `start`, `end` and
`title`, so the user can recreate it with `calendar_event_update` or `calendar_event_create`.
This is the acceptance criterion "listed in the result, not silently dropped".

### 6. The result

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
  "previous": {"uid": "...", "last_start": "..."}                       # *_from only
}
```

`id` and the times are those of the first occurrence of the changed part, formatted by the
read path's own code (as the existing tools do), and readable with `calendar_event` at once.
`occurrences` counts the changed part's occurrences after its deleted ones are removed, up to
its last occurrence, or, for an endless series, through 366 days after its first occurrence,
in which case `occurrences_through` is that last day counted. `unmatched` is not capped: its
length is bounded by the exceptions stored in one resource, and its count is also in
`exceptions.unmatched`. Times in the result are in the event's zone, as everywhere else.
Titles in `unmatched` are event content and the result keeps the read tools'
`content_trust: untrusted` marking.

### 7. Order of writes and rollback (`_from` modes)

1. Fresh read of the series' resource (single-href `calendar-multiget`, data and ETag);
   writable-calendar, people and series-shape checks; both resources computed. Nothing is
   sent if any check fails.
2. With `preview`, return the result here.
3. `PUT` of the new resource with `If-None-Match: *`. If it fails, nothing changed (or, when
   no answer arrived, the error says so as today).
4. `PUT` of the cut series with `If-Match` = the ETag from step 1.
5. If step 4 is answered with 412, 404 or another 4xx (iCloud made no change to the old
   resource), the new resource is read once (single-href `calendar-multiget`) and deleted
   with `DELETE` and `If-Match` = that ETag, then the call fails with step 4's error (for
   412: `the event changed in Calendar; read it again`). The calendar is as it was.
6. If step 4 gets no answer, is throttled, or the rollback in step 5 fails, nothing more is
   sent and the call fails with an error saying that the series from `<effective>` may now
   be stored twice, naming the new series' `id`, and that the user should check it in
   Calendar. No step is retried.

Creating first means the likeliest failure, a conflict on the existing resource, can be
undone by removing a resource this call itself created a moment ago. Cutting first would mean
a failed create loses every future occurrence. The rollback `DELETE` meets ADR-0005's rules
(one event resource, `If-Match` from a read in the same call, `Schedule-Reply: F`). A call
sends at most three write requests. Between steps 3 and 4 another client could briefly see
the occurrences from the date twice.

### 8. Preview

`preview: true` runs steps 1 and 2: it reads, checks and computes everything and returns the
same result with `preview: true`, sending no write. It exists because a series edit can
discard many exceptions and drop unmatched ones; the description tells clients to preview
and confirm with the user when exceptions would be discarded or unmatched. A preview is still
a call to a write tool, so it counts against the token's hourly write limit like any other
call (one budget, one rule); see Open Questions.

### 9. ADR-0007 (prerequisite)

The implementation waits for an ADR, written and merged by the maintainer the way ADR-0005
was (#28). What it would decide, for review here:

1. A calendar write call may change one whole series, or one series from a date on, through
   `calendar_series_update`; `calendar_event_update` and `calendar_event_delete` keep "one
   event or occurrence per call". This amends ADR-0005 decision 3.
2. A series edit "from a date" may send, in one call, a `PUT` with `If-None-Match: *` of one
   new event resource and a `PUT` with `If-Match` of the series' resource, plus, only to undo
   the first after the second was refused, a `DELETE` with `If-Match` of that new resource.
   No other method, no other resource; ADR-0005's method list and preconditions are
   unchanged.
3. Everything else in ADR-0005 applies unchanged: writable and unprotected calendars, no
   attendees and no events involving anyone else, `Schedule-Reply: F`, 412 reported and never
   retried, one write-limit budget (a series edit counts once), redacted audit.

CLAUDE.md's calendar rules remain true as written (each request is one event resource);
whether to add a line about series edits is the maintainer's call in that ADR's pull request.

### 10. Audit, limits, instructions

Audit lines are as for every calendar write: `write: true`, every argument redacted
(`mode` and `effective` included; `preview` as `<set>`), `result_count` 1. Field limits are
those of the existing tools; `effective` must be an ISO date; `mode` one of the four values.
The rate-limit check runs first, as for every write tool. The instructions for a token with
`calendar_write` name the new tool and say it changes a whole series or a series from a
date, that it can discard deleted and changed occurrences, and that it should be previewed
and confirmed with the user.

## Risks / Trade-offs

- **One call changes many occurrences** → `preview`, description and instructions that ask
  for confirmation, protected calendars, the switch and grant, and a destructive annotation.
- **Split series look like two events to some clients** (different `UID`s) → this is how
  calendar apps store "this and future"; to be confirmed on iCloud and Apple's apps (Open
  Questions). The previous part keeps its `UID`, so links to past occurrences still work.
- **Brief duplicate between the two writes, or a lasting one if the second gets no answer**
  → ordered so the likely failure is rolled back; the unlikely one is reported with the new
  series' id, never retried.
- **Unmatched exceptions are dropped from the series** → listed in the result with enough
  data to recreate them, and visible in a preview first.
- **Rules beyond `calendar_event_create`'s subset** → kept when `recurrence` is not given,
  replaced when it is; matching uses the rule as stored via `cal/expand.py`.

## Migration Plan

None: a new tool for tokens that already have `calendar_write`. Rollback is removing the
tool; stored series written by it are ordinary iCalendar data.

## Open Questions

1. **ADR-0007**: does the maintainer accept a split into two resources and the rollback
   `DELETE` (decisions 4, 7, 9), and land the ADR before implementation?
2. **Live check on iCloud** (as done for ADR-0005): after a split, do iCloud web and Apple's
   apps show both parts as expected, and does editing either part in Calendar behave? Does
   iCloud accept a `COUNT`/`UNTIL` cut and a new resource whose overrides carry re-keyed
   `RECURRENCE-ID`s?
3. **Preview and the write limit**: should `preview: true` count against
   `max_writes_per_hour` (proposed: yes, one rule for all write-tool calls), or be exempt
   since it sends no write?
4. **Unmatched changed occurrences**: drop and list them (proposed), or refuse the whole
   call when any changed occurrence would be unmatched unless the caller passes an explicit
   flag such as `drop_unmatched: true`?
5. **Propagating changed fields to overrides** (decision 5): is "take the new value only
   where the override still had the master's value" the wanted behaviour for `update_*`?
