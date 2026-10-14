# calendar-write-tools Specification

## Purpose
The opt-in calendar write tools (ADR-0005): `calendar_event_create`,
`calendar_event_update` and `calendar_event_delete`, and the series edits of
`calendar_series_update` (ADR-0007); who may use them, their arguments, results and limits,
single-occurrence edits and deletes, whole-series and from-a-date edits (Apple's split
format and recurrence sets), conditional requests after a fresh read, the calendars they may
write to, and the rule that no write ever makes iCloud email anyone.

## Requirements

### Requirement: Calendar write tools exist only for granted tokens
`calendar_event_create`, `calendar_event_update`, `calendar_event_delete` and
`calendar_series_update` SHALL be available to a caller only when `PV_ICLOUD_CALENDAR_WRITE`
is `enabled` and the caller's token has `calendar_write: true`. For any other caller they
SHALL be absent from `tools/list`, and a call to one SHALL fail with the same tool error as a
call to a tool that does not exist, without contacting iCloud. Mail write grants SHALL NOT
grant calendar writes, and `calendar_write` SHALL NOT grant any mail write.

#### Scenario: Token with calendar write
- **WHEN** a token has `calendar_write: true` and calls `tools/list`
- **THEN** the list contains every read tool and the four calendar write tools, and no mail
  write tool

#### Scenario: Token without calendar write
- **WHEN** a token without `calendar_write` calls `calendar_event_delete` or
  `calendar_series_update` directly
- **THEN** the call fails as an unknown tool and iCloud sees no request

#### Scenario: Two tokens for one account
- **WHEN** token `laptop` has `calendar_write: true` and token `phone` has not, both for the
  same account
- **THEN** `laptop` can create events and `phone` sees only the read tools

### Requirement: Calendar writes share the per-token write rate limit
Every call to a calendar write tool SHALL count against the calling token's
`max_writes_per_hour` over the same sliding one-hour window and the same per-token budget as
mail write calls, whatever the call's outcome. The check SHALL run before any other check and
before any CalDAV request. A call over the limit SHALL fail with the tool error `write limit
reached for this token (N per hour); try again in about M minutes` and SHALL send nothing.

#### Scenario: Limit reached
- **WHEN** a token with `max_writes_per_hour: 2` has made two write calls in the last hour,
  of any kind, and calls `calendar_event_create`
- **THEN** the call fails with `write limit reached for this token (2 per hour)` and iCloud
  sees no request

### Requirement: Writable calendars
A calendar write SHALL reach only a calendar that is all of: in the token's scope and of kind
`calendar`, belonging to the caller's account; not subscribed; not protected; and whose
`current-user-privilege-set`, read with the calendar list, includes `write-content` (directly
or through `write` or `all`). A calendar is protected when its display name is exactly one of
the names in the account's protected calendars (see `accounts-and-auth`) or the caller
token's `protected_calendars`. A calendar name or id, or an event id, that names any other
calendar (excluded, missing, another account's, subscribed, protected or without
`write-content`) SHALL fail with `unknown calendar: <name as given>`, the same error in every
case, and nothing SHALL be sent. An event id whose resource path is not a direct child of its
calendar's collection SHALL fail with `malformed event id`. The server SHALL NOT rely on
iCloud answering HTTP 403. Protected calendars and calendars without `write-content` SHALL
stay readable by the read tools.

#### Scenario: Excluded calendar
- **WHEN** `Banking` is excluded and a caller creates an event in `Banking`
- **THEN** the call fails with `unknown calendar: Banking` and no write request is sent

#### Scenario: Another account's calendar
- **WHEN** a token of account `alice` passes an event id whose calendar id belongs only to
  account `bob`
- **THEN** the call fails with `unknown calendar`, exactly as for a calendar that does not
  exist, and `bob`'s CalDAV host sees no request

#### Scenario: Subscribed calendar
- **WHEN** a caller creates an event in the subscribed calendar `TripIt`
- **THEN** the call fails with `unknown calendar: TripIt` and no write request is sent

#### Scenario: Calendar shared read-only
- **WHEN** a calendar shared with the account lacks `write-content` and a caller deletes one
  of its events
- **THEN** the call fails with `unknown calendar`, no `DELETE` is sent, and
  `calendar_event` still returns the event

#### Scenario: Calendar shared read-write
- **WHEN** a calendar shared with the account grants `write-content` and is not protected
- **THEN** a caller can create an event in it

#### Scenario: Protected calendar
- **WHEN** the account protects `Family` and a caller with `calendar_write` updates an event
  in `Family`
- **THEN** the call fails with `unknown calendar`, nothing is sent, and the event is still
  readable

#### Scenario: Token's own protected calendar
- **WHEN** token `agent` has `protected_calendars: [Work]` and token `laptop` has none
- **THEN** `agent` cannot write to `Work` and `laptop` can

### Requirement: calendar_event_create
`calendar_event_create` SHALL create one event in the given calendar (name, case-insensitive,
or id) as a new resource whose name and `UID` are a new random UUID, with `If-None-Match: *`.
It SHALL take `title` (required), `start` (required), and optionally `end`, `all_day`,
`timezone`, `location`, `notes`, `alerts` and `recurrence`; it SHALL take no attendees. A
timed event SHALL be written with `DTSTART` and `DTEND` carrying `TZID` of `timezone`
(default: the account's default time zone, as the read tools use) and a matching `VTIMEZONE`;
a value with an offset SHALL be converted into that zone and a value without one read in it.
A timed `end` SHALL default to one hour after `start`. An all-day event SHALL take dates,
with `end` the inclusive last day (default `start`), written as date `DTEND` the day after.
`recurrence` SHALL be written as one `RRULE`. The result SHALL include `id`, `uid`,
`calendar`, `calendar_id`, `start`, `end`, `timezone`, `all_day` and `recurring`, in the
formats of `calendar_event`, with `id` naming the event's first occurrence and readable by
`calendar_event` at once.

#### Scenario: Timed event in another zone
- **WHEN** a caller creates `Dentist` from `2026-11-03T09:00` to `2026-11-03T10:00` with
  `timezone` `Europe/London`
- **THEN** iCloud stores one `VEVENT` with `DTSTART;TZID=Europe/London:20261103T090000` and a
  `VTIMEZONE` for `Europe/London`, and `calendar_event` with the returned `id` shows it

#### Scenario: All-day event
- **WHEN** a caller creates an all-day event with `start` `2026-12-24` and `end` `2026-12-26`
- **THEN** iCloud stores `DTSTART;VALUE=DATE:20261224` and `DTEND;VALUE=DATE:20261227`, and
  the result's `end` is `2026-12-26`

#### Scenario: Weekly recurring event
- **WHEN** a caller creates `Standup` with `recurrence` `{frequency: weekly, weekdays: [MO,
  WE], count: 10}`
- **THEN** iCloud stores `RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO,WE` and the result has
  `recurring` true

#### Scenario: Attendees passed anyway
- **WHEN** a caller passes an `attendees` argument to `calendar_event_create`
- **THEN** the call fails with a tool error and iCloud sees no request

#### Scenario: Name already taken
- **WHEN** iCloud answers the `PUT` with HTTP 412
- **THEN** the call fails, and no existing resource is replaced

### Requirement: calendar_event_update
`calendar_event_update` SHALL change only the fields it is given (`title`, `start`, `end`,
`all_day`, `timezone`, `location`, `notes`, `alerts`) and remove those listed in `clear`
(`location`, `notes`, `alerts`); a field both given and cleared SHALL be a tool error. It
SHALL take no attendees. For a single event it SHALL change that event. For an occurrence of
a recurring series it SHALL change that occurrence only: by editing its existing override, or
by adding an override whose `RECURRENCE-ID` is the occurrence's original start (same value
type and `TZID` as the master's `DTSTART`), copied from the master without `RRULE`, `RDATE`
and `EXDATE`. Every other component and property of the resource SHALL be kept as stored. The
component changed SHALL get a higher `SEQUENCE` and a new `LAST-MODIFIED`. The result SHALL
have the same shape as `calendar_event_create`'s, with `id` naming the changed event or
occurrence.

#### Scenario: Change one field
- **WHEN** a caller updates only the `location` of a single event that has notes, alerts and
  an `X-APPLE-TRAVEL-ADVISORY-BEHAVIOR` property
- **THEN** only `LOCATION`, `SEQUENCE` and `LAST-MODIFIED` differ in the stored resource

#### Scenario: Edit one occurrence
- **WHEN** a caller moves the second occurrence of a weekly series one hour later
- **THEN** the resource gains an override for that occurrence's original start with the new
  times, and the first and third occurrences are unchanged

#### Scenario: Edit an occurrence that already has an override
- **WHEN** the occurrence being changed already has an override
- **THEN** that override is edited and no second override for the same `RECURRENCE-ID` is
  added

#### Scenario: Clearing notes
- **WHEN** a caller passes `clear: [notes]`
- **THEN** the event no longer has `DESCRIPTION`

### Requirement: calendar_event_delete
`calendar_event_delete` SHALL delete one event or one occurrence. For a single event, or for
a resource that holds only the override named by the id, it SHALL send `DELETE` for the
resource. For an occurrence of a recurring series it SHALL add an `EXDATE` for the
occurrence's original start to the master (same value type and `TZID` as its `DTSTART`),
remove any override for that occurrence, and keep everything else. It SHALL NOT delete a
whole series in one call. The result SHALL have `deleted` (`event` or `occurrence`) and
`calendar`. The tool's description SHALL say that a deleted event cannot be restored by
pv-icloud.

#### Scenario: Delete a single event
- **WHEN** a caller deletes a non-recurring event
- **THEN** iCloud receives one `DELETE` with `If-Match` for its resource, and `deleted` is
  `event`

#### Scenario: Delete one occurrence
- **WHEN** a caller deletes the second occurrence of a weekly series
- **THEN** the master gains an `EXDATE` for that start, `calendar_events` no longer returns
  it, the other occurrences are still returned, and `deleted` is `occurrence`

### Requirement: Conditional writes after a fresh read
Every `PUT` and `DELETE` SHALL carry `If-None-Match: *` (new resource) or `If-Match`; a
request without one SHALL be refused before it is sent. Before every update and delete the
server SHALL re-read that one event resource from iCloud, stored data and ETag, without
expansion, with one `calendar-multiget` `REPORT` naming only that resource, and SHALL send `If-Match` with exactly that ETag; an ETag SHALL never be reused
from an earlier call. The occurrence named by the id SHALL be found in the data just read
(expanding the series locally when needed); when the resource or the occurrence is not there
the call SHALL fail with a tool error mentioning `not found` and send no write. When iCloud
answers HTTP 412 the call SHALL fail with the tool error `the event changed in Calendar; read
it again`, and nothing SHALL be changed. A write SHALL never be retried automatically; when no
answer arrives after the request was sent, the tool error SHALL say iCloud did not confirm the
change and the event should be read again before retrying.

#### Scenario: Event changed meanwhile
- **WHEN** the event's ETag changes between the fresh read and the `PUT`, for example because
  an attendee's reply changed it on the server
- **THEN** iCloud answers 412, the call fails with `the event changed in Calendar; read it
  again`, no second `PUT` is sent, and the stored event is the other version

#### Scenario: Event already gone
- **WHEN** a caller updates an event that was deleted in Calendar
- **THEN** the call fails with a tool error mentioning `not found` and no `PUT` is sent

#### Scenario: Occurrence already removed
- **WHEN** a caller updates an occurrence whose start is already in the master's `EXDATE`
- **THEN** the call fails with a tool error mentioning `not found` and no `PUT` is sent

### Requirement: No email to anyone
No calendar write SHALL cause iCloud to send an invitation, update, cancellation or reply to
anyone. The server SHALL NOT write `ORGANIZER` or `ATTENDEE` properties, and every write
request SHALL carry `Schedule-Reply: F`. Update, delete and series edits SHALL be refused,
after the fresh read and before any write request, when any event in a stored resource the
call would write (the event or occurrence concerned, the series master or another
occurrence's override: a `PUT` rewrites them all; for a series edit, every part of the
series' recurrence set) has an `ORGANIZER` that is not
the account itself (`this event was organised by someone else; change it in Calendar`) or any
`ATTENDEE` that is not the account itself (`event has attendees; change it in Calendar`). A
value is the account itself when it is a `mailto:` address in the account principal's
`calendar-user-address-set`, or a principal path (a non-`mailto:` value) that equals the
account's principal URL or whose `EMAIL=` parameter is one of those addresses; addresses
compare case-insensitively, and any other value counts as someone else. A new resource
created by a series edit SHALL carry no `ORGANIZER` or `ATTENDEE`. Alerts SHALL be written
only with `ACTION:DISPLAY`.

#### Scenario: Event with an external attendee
- **WHEN** a caller changes the title of an event the account organises that has the
  attendee `carol@example.com`
- **THEN** the call fails with `event has attendees; change it in Calendar`, no `PUT` is sent,
  and the fake server records no scheduling message

#### Scenario: Invitation from someone else
- **WHEN** a caller deletes an event whose `ORGANIZER` is `mailto:someone@example.org`, not an
  address of the account
- **THEN** the call fails saying the event was organised by someone else, and no `PUT` or
  `DELETE` is sent

#### Scenario: Account's own principal path
- **WHEN** an event's only `ORGANIZER` and `ATTENDEE` are the account's principal path with
  `EMAIL=` set to one of the account's addresses
- **THEN** a caller can update and delete it

#### Scenario: Principal path without a known address
- **WHEN** an event has an `ATTENDEE` that is a principal path with no `EMAIL=` parameter and
  not the account's principal URL
- **THEN** update and delete are refused with `event has attendees; change it in Calendar`

#### Scenario: Series with attendees
- **WHEN** a recurring series' master has an external attendee and a caller deletes one
  occurrence
- **THEN** the call is refused and no `EXDATE` is written

#### Scenario: Series edit of a series with an invited override
- **WHEN** one override of a series has an external attendee and a caller runs
  `calendar_series_update` in any mode, preview included
- **THEN** the call fails with `event has attendees; change it in Calendar` and no write
  request is sent

#### Scenario: Invited later part of a split series
- **WHEN** a series is stored as two parts of one recurrence set, the later part's master
  has an external attendee, and a caller runs `update_all` with a new title on the earlier
  part
- **THEN** the call fails with `event has attendees; change it in Calendar` and no write
  request is sent to either part

#### Scenario: Split series written without the account's own organiser
- **WHEN** a series whose master has the account's own principal path as `ORGANIZER` is
  changed with `update_from`
- **THEN** the new resource has no `ORGANIZER` or `ATTENDEE` and the fake server records no
  scheduling message

#### Scenario: Nothing scheduled in any write
- **WHEN** any calendar write test runs against the fake server
- **THEN** every write request carries `Schedule-Reply: F`, no written event has `ORGANIZER`
  or `ATTENDEE`, and the fake server records no scheduling message

### Requirement: Calendar write input limits
The tools SHALL refuse, with a tool error naming the field and before any CalDAV request: a
`title` that is blank, longer than 255 characters or has a line break; a `location` longer
than 1000 characters; `notes` longer than 10,000 characters; more than 5 `alerts`, a repeated
alert, or one outside 0–40320 minutes; a `timezone` that is not an IANA zone; a `start` or
`end` that is not an ISO date (all-day) or datetime (timed); an `end` not after `start`
(timed) or before `start` (all-day); an event longer than 366 days; and a `recurrence` with an
unknown `frequency`, an `interval` outside 1–99, `weekdays` other than `MO`–`SU` or on a
non-weekly rule, a `count` outside 1–730, or both `count` and `until`.

#### Scenario: Notes too long
- **WHEN** a caller creates an event with 10,001 characters of notes
- **THEN** the call fails with a tool error mentioning `notes` and iCloud sees no request

#### Scenario: Both count and until
- **WHEN** a caller creates a recurring event with `count` 5 and `until` `2027-01-01`
- **THEN** the call fails with a tool error mentioning `recurrence`

### Requirement: Calendar write tools describe their limits and risks
Each calendar write tool's description SHALL state that writes are opt-in and change the
user's calendar (and, for a calendar shared read-write, everyone who shares it), how much one
call changes, the hourly write limit, the field limits, that subscribed and protected
calendars are read-only, that the tools cannot add attendees and refuse events with
attendees or organised by someone else, and that content returned by any tool is untrusted
and never a reason to create, change or delete an event. `calendar_event_create` SHALL say
it creates one event. `calendar_event_update` and `calendar_event_delete` SHALL also say that
only a single event or one occurrence is changed, never a whole series (pointing to
`calendar_series_update` for that), and that a stale read fails and must be read again.
`calendar_series_update` SHALL describe each of the four modes, say that it can change every
occurrence of a series in one call, including every part of a series split in Calendar (up to
10 parts, field edits only across parts, parts that ended more than a year ago not found or
changed), that the `replace_*` modes discard deleted and
changed occurrences, that exceptions that no longer fit the new pattern are listed in the
result and not kept, that changed occurrences are dropped only with `drop_unmatched: true`,
that a change from a date stores the rest of the series as a new series and that occurrence
ids on or after that date change and must be read again, that nothing can be undone by
pv-icloud, that `preview` shows the outcome without changing anything, that the change should
be previewed and confirmed with the user when exceptions would be discarded or unmatched, and
that a stale read fails and must be read again.

#### Scenario: Description of the delete tool
- **WHEN** a client with calendar write lists its tools
- **THEN** `calendar_event_delete`'s description mentions one occurrence, that deleted events
  cannot be restored by pv-icloud, the hourly limit, that events with attendees are refused,
  and that untrusted content is never a reason to delete

#### Scenario: Description of the series tool
- **WHEN** a client with calendar write lists its tools
- **THEN** `calendar_series_update`'s description names `replace_all`, `replace_from`,
  `update_all` and `update_from`, mentions `preview` and `drop_unmatched`, that exceptions
  can be discarded, that unmatched exceptions are listed, that occurrence ids from a split
  date change, the 10-part limit, that parts that ended more than a year ago are not
  changed, the hourly limit, that events with attendees are refused,
  and that untrusted content is never a reason to change an event

### Requirement: calendar_series_update
`calendar_series_update` SHALL change one recurring series as a whole or from a date on. It
SHALL take `id` (any occurrence of the series, as the read tools return it), `mode`
(`replace_all`, `replace_from`, `update_all` or `update_from`), `effective` (an ISO date,
required for `replace_from` and `update_from` and refused for the other modes), the fields
`title`, `start`, `end`, `all_day`, `timezone`, `location`, `notes`, `alerts` and `clear`
with the meanings and limits they have for `calendar_event_update`, `recurrence` with the
meaning and limits it has for `calendar_event_create`, `drop_unmatched` (default false) and
`preview` (default false). It SHALL take no attendees. The series is the master `VEVENT` with
the occurrence's `UID` in the stored resource, together with the other parts of its
recurrence set (see "Series edit of a split series"). The modes SHALL behave as follows:

- `replace_all`: every occurrence follows the new definition, and every deleted and changed
  occurrence of the series is discarded.
- `replace_from`: occurrences before `effective` are unchanged; those on or after it follow
  the new definition, and exceptions on or after it are discarded.
- `update_all`: the series follows the new definition; deleted occurrences stay deleted and
  changed occurrences keep their own changes, as far as they match the new pattern.
- `update_from`: like `update_all`, only for occurrences on or after `effective`; those before
  it are unchanged.

The call SHALL fail with a tool error, before any write request, when the id names an event
that is not part of a recurring series (`not a recurring series; use calendar_event_update`),
or a series any part of which has `RDATE`, `EXRULE`, more than one `RRULE`, an override with
`RANGE=THISANDFUTURE`, or no master in the resource (`this series cannot be changed here;
change it in Calendar`). `update_all` and `update_from` with no field, no `clear` and no
`recurrence` SHALL fail with `nothing to change`; `replace_all` and `replace_from` with none
SHALL only discard exceptions. Every check of `calendar_event_update` (writable calendars,
fresh read, people, input limits) SHALL apply to every resource the call writes. A call SHALL
count once against the token's hourly write limit, whatever it sends, a preview included.

#### Scenario: Replace a whole series
- **WHEN** a weekly series with two deleted and two changed occurrences is changed with
  `replace_all` and a new `title`
- **THEN** the stored resource has one master with the new title, no `EXDATE` and no
  override, every occurrence `calendar_events` returns has the new title, and the result's
  `exceptions.discarded` is 4

#### Scenario: Update a whole series keeping exceptions
- **WHEN** a weekly series with two deleted occurrences and one moved occurrence is changed
  with `update_all` and a new `location`
- **THEN** the two deleted occurrences are still absent, the moved occurrence is still at
  its moved time, every other occurrence has the new location, and `exceptions.kept` is 3

#### Scenario: Not a series
- **WHEN** a caller passes the id of a single event
- **THEN** the call fails with `not a recurring series; use calendar_event_update` and no
  write request is sent

#### Scenario: Series with RDATE
- **WHEN** the stored series has an `RDATE`
- **THEN** the call fails with `this series cannot be changed here; change it in Calendar`
  and no write request is sent

#### Scenario: Effective date with a whole-series mode
- **WHEN** a caller passes `effective` with `mode` `update_all`
- **THEN** the call fails with a tool error mentioning `effective` and iCloud sees no request

#### Scenario: Reset a series
- **WHEN** a caller runs `replace_all` with no field on a series with exceptions
- **THEN** the series keeps its definition and loses every `EXDATE` and override

### Requirement: Series edit definition
Fields not given SHALL keep the series' current values in every mode. `title`, `location`,
`notes`, `alerts`, `clear`, `timezone` and `all_day` SHALL apply to the series' definition.
`start` and `end` SHALL set the times of the first changed occurrence: the series' first
occurrence in the `_all` modes, the first occurrence on or after `effective` in the `_from`
modes, where a `start` dated before `effective` SHALL be a tool error. A new `start` without
`end` SHALL keep the series' length, and changing only `timezone` SHALL keep the wall-clock
times. `recurrence` SHALL replace the series' rule entirely, its `count` or `until` bounding
the changed part; without it the current rule SHALL be kept (ended at the date in the
`_from` modes). The changed series SHALL begin with the first occurrence the new rule
produces on or after `start`, or, without `start`, on or after the current first occurrence
(`_all`) or the first original occurrence on or after `effective` (`_from`), at the new time
of day, so that no occurrence outside the rule is added. A definition that yields no
occurrence SHALL be a tool error. Changed components SHALL get a higher `SEQUENCE` and a new
`LAST-MODIFIED`, and new times SHALL be written with `TZID` and a matching `VTIMEZONE`.
`start`, `end`, `recurrence`, `all_day` and `timezone` are pattern fields; `title`,
`location`, `notes`, `alerts` and `clear` are field edits.

#### Scenario: New weekdays
- **WHEN** a weekly `MO,WE` series starting on a Monday is changed with `update_all` and
  `recurrence` `{frequency: weekly, weekdays: [TU, TH]}`
- **THEN** the stored master has `RRULE:FREQ=WEEKLY;BYDAY=TU,TH` and a `DTSTART` on the first
  Tuesday on or after the old first Monday, and no Monday occurrence is returned

#### Scenario: New time of day
- **WHEN** a daily 09:00 series is changed with `update_all` and `start` set to the first
  occurrence's date at 10:00
- **THEN** every occurrence starts at 10:00 and keeps its length

#### Scenario: Start before the effective date
- **WHEN** a caller runs `update_from` with `effective` `2027-01-11` and `start`
  `2027-01-04T09:00`
- **THEN** the call fails with a tool error mentioning `start` and nothing is sent

### Requirement: Series edit exceptions
An exception SHALL be an `EXDATE` value (kind `deleted`) or an override (kind `changed`; an
override with `STATUS:CANCELLED` is kind `deleted`). Its original start (the `EXDATE` value or
the override's `RECURRENCE-ID`), not where a changed occurrence was moved to, SHALL decide
whether it is before `effective`; days SHALL be taken in the series' zone (`DTSTART`'s
`TZID`, or the account's default zone for all-day and floating series). In the `replace_*`
modes every exception in range SHALL be discarded. In the `update_*` modes an exception in
range SHALL be kept when the new series has exactly one occurrence on the day of its original
start, re-keyed to that occurrence's start (the `EXDATE` value or the override's
`RECURRENCE-ID`, in the value type and `TZID` of the new `DTSTART`). Every override the
call creates or re-keys SHALL carry exactly the master's
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value (none when the master has none);
overrides the call does not create or re-key SHALL keep that property as stored. A kept override SHALL
keep its own changes: for each of `title`, `location`, `notes` and `alerts` that the call
changes, it SHALL take the new value only when its own value equalled the master's before the
call (both absent counts as equal); an override whose start equals its original start and
whose length equals the series' length SHALL take its occurrence's new times, and any other
override SHALL keep its own start and end. When the call changes `all_day`, no override SHALL
be kept. An exception in range that is not kept in an `update_*` mode is unmatched and SHALL
always be listed in the result's `unmatched`, a preview included. An unmatched exception of
kind `deleted` SHALL be removed from the series. When any exception of kind `changed` is
unmatched and `drop_unmatched` is not true, the call SHALL fail before any write request
with `<n> changed occurrences no longer fit the new pattern; preview to see them, or pass
drop_unmatched: true to drop them`, an error that holds no event content; with
`drop_unmatched: true` such exceptions SHALL be removed from the series. A preview SHALL NOT
fail for this reason; it SHALL return the result with `requires_drop_unmatched` true.

#### Scenario: Moved occurrence keeps its time
- **WHEN** a 09:00 daily series has an occurrence moved to 14:00 and is changed with
  `update_all` and a new title
- **THEN** that occurrence is still at 14:00 and has the new title, unless its own title had
  been changed, in which case it keeps that title

#### Scenario: New notes reach overrides without their own
- **WHEN** a series has two overrides without their own notes and one retitled override, and
  is changed with `update_all` and new `notes`
- **THEN** all three overrides have the new notes and the retitled one keeps its title

#### Scenario: New time of day re-keys exceptions
- **WHEN** a 09:00 daily series with a deleted occurrence on 2027-02-15 is changed to 10:00
  with `update_all`
- **THEN** the master's `EXDATE` is 2027-02-15 at 10:00, no occurrence is returned on that day,
  and `unmatched` is empty

#### Scenario: Re-keyed override carries the recurrence set
- **WHEN** a series whose master carries a recurrence-set `RELATED-TO` that no other
  resource shares has an override without it, and is changed with `update_all` and a new
  time of day
- **THEN** the re-keyed override carries the master's recurrence-set value

#### Scenario: Deleted occurrence that no longer fits
- **WHEN** a weekly `MO,WE` series with a changed Monday occurrence and a deleted Wednesday
  occurrence is changed with `update_all` to `weekdays: [MO]`
- **THEN** the changed Monday occurrence is kept, the deleted Wednesday is listed in
  `unmatched` with `kind` `deleted` and its `original_start`, and `exceptions.unmatched` is 1

#### Scenario: Changed occurrence that no longer fits
- **WHEN** a changed occurrence's day has no occurrence in the new pattern in `update_from`
  and `drop_unmatched` is not given
- **THEN** the call fails with `1 changed occurrences no longer fit the new pattern; preview
  to see them, or pass drop_unmatched: true to drop them` and no write request is sent

#### Scenario: Previewing an unmatched changed occurrence
- **WHEN** the same call is made with `preview: true`
- **THEN** the result lists the occurrence in `unmatched` with `kind` `changed`,
  `original_start`, its current `start`, `end` and `title`, and has
  `requires_drop_unmatched` true

#### Scenario: Dropping an unmatched changed occurrence
- **WHEN** the same call is made with `drop_unmatched: true`
- **THEN** the occurrence is stored in neither part of the series and is listed in
  `unmatched`

### Requirement: Series edit from a date
In the `_from` modes the part of the series that contains `effective` SHALL be stored as two
series, in Apple Calendar's format. The existing resource SHALL keep its `UID` and every
occurrence and exception before `effective` as they were: its master's rule SHALL get an
`UNTIL` of 23:59:59 local time, in the series' zone, on the day before `effective`, written
in UTC for a zoned series (for example `20261103T055959Z` for an America/Chicago series and
`effective` 2026-11-03), as a local time for a floating series and as the date of that day
for an all-day series, replacing any `COUNT`; its `EXDATE` values and overrides on or after
`effective` SHALL be removed, and every other component and property SHALL be kept as stored.
The part from `effective` on SHALL be a new series in a new resource of the same calendar,
whose name and `UID` are a new random UUID, copying every property and alarm of the master
(among them `LOCATION`, `X-APPLE-STRUCTURED-LOCATION` and `TRANSP`) with the new definition
applied, with the rule given (or the rest of the old one: its `UNTIL`, or its `COUNT` minus
the occurrences generated before the date) and, in `update_from`, the kept exceptions
re-keyed and given the new `UID`. Both masters SHALL carry
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` with the same value: the old master's
value when it has one, otherwise a new random UUID unrelated to either `UID`; the kept
overrides in the new resource SHALL carry it too, and the old resource's remaining overrides
SHALL be kept as stored. The call SHALL fail with a tool error before any write when the
series has no occurrence on or after `effective` (`the series has no occurrence on or after
<date>`). When `effective` is on or before the first occurrence of the part that would be
cut (the first part found from it), nothing SHALL be split: that part SHALL be changed in
place with one `PUT` with `If-Match` and no new resource, whether or not its master carries a
recurrence-set value, and the result's `parts.split` SHALL be false. After a split the
result SHALL carry `ids_changed_from`, the effective date, and the tool description SHALL
say that occurrence ids on or after it change.

#### Scenario: Earlier occurrences untouched
- **WHEN** a school-day series with a deleted occurrence and a moved occurrence before
  2027-01-11 and the same after it is changed with `replace_from` and `effective`
  `2027-01-11` and a new start time
- **THEN** `calendar_events` for the weeks before 2027-01-11 returns exactly what it returned
  before the call, the stored old resource's earlier override is unchanged, and from
  2027-01-11 every occurrence has the new time and no exception remains

#### Scenario: Keeping exceptions from a date
- **WHEN** the same series is changed with `update_from` and `effective` `2027-01-11` and a
  new location
- **THEN** occurrences before the date are as before, the deleted occurrence after the date
  is still absent, the moved occurrence after it keeps its time and gains the new location,
  and both are in the new resource with the new `UID`

#### Scenario: Apple's cut
- **WHEN** an America/Chicago weekly series is changed with `update_from` and `effective`
  `2026-11-03`
- **THEN** the old master's rule has `UNTIL=20261103T055959Z`, both masters carry the same
  `RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` value, which equals neither `UID`,
  and the result's `ids_changed_from` is `2026-11-03`

#### Scenario: All-day cut
- **WHEN** an all-day weekly series is changed with `update_from` and `effective`
  `2027-03-01`
- **THEN** the old master's rule has `UNTIL=20270228` as a `DATE`

#### Scenario: Recurrence set value reused
- **WHEN** the series' master already has a recurrence-set `RELATED-TO` and is split again
- **THEN** the new master carries the same value and the old master's value is unchanged

#### Scenario: Count split
- **WHEN** a weekly series with `COUNT=10` is changed with `update_from` at its fifth
  occurrence without `recurrence`
- **THEN** the old master's rule has an `UNTIL` on the day before and no `COUNT`, the new
  series has `COUNT=6`, and together they return the same ten dates as before

#### Scenario: Effective date at the first occurrence
- **WHEN** `effective` is on or before the first occurrence of a series without a
  recurrence-set `RELATED-TO` and the call is `update_from` with a new title
- **THEN** the series' resource is changed in place with one `PUT` with `If-Match`, no new
  resource is created, every occurrence has the new title, and the result's `parts.split` is
  false and has no `ids_changed_from`

### Requirement: Series edit of a split series
A series' recurrence set SHALL be every resource in the series' calendar whose master carries
`RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET` with the same value as the series'
master; a series without that property is a set of one part, and no other part is looked
for. The server SHALL find the other parts with `calendar-query` `REPORT`s of the series'
calendar for `VEVENT`s in a `time-range`, returning `getetag` and unexpanded data, and SHALL
match their masters' recurrence-set value locally; it SHALL NOT send a `prop-filter`, a
`param-filter` or a `collation`. It SHALL list:

- later parts, in every mode, from the named part's last occurrence (in the `_all` modes;
  skipped when the named part has no last occurrence) or from `effective` (in the `_from`
  modes), with no end;
- earlier parts, in the `_all` modes only, from 366 days before the current date to the named
  part's first occurrence (skipped when that occurrence is before the window's start).

Parts before that window, and in the `_from` modes parts before `effective`, SHALL NOT be
looked for or written. A part's data and ETag from the listing SHALL serve as its fresh read.
The server SHALL refuse more than 10 parts found before any write with `this series has more
than 10 parts; change it in Calendar`. On a set of which more than one part is found:

- `update_all` that changes only field edits SHALL apply them to every part, each part with
  one `PUT` with `If-Match` of its own fresh read, the overrides of each part treated as in
  "Series edit exceptions".
- `replace_all`, and `update_all` that changes a pattern field, SHALL fail before any write
  with `this series was split; change it from a date (replace_from / update_from)`.
- The `_from` modes SHALL change the part that contains `effective` as in "Series edit from a
  date", except that when `effective` is on or before the first occurrence of a part found,
  that part SHALL be changed in place with one `PUT` with `If-Match` and no new resource. Every part that begins after it SHALL be treated like
  `update_all`: field edits SHALL be applied to it with one `PUT` with `If-Match`; when there
  is such a part, a pattern field or `replace_from` SHALL fail before any write with `this
  series was split on <date>; change its pattern from that date or later`. Parts before the
  one containing `effective` SHALL NOT be written.

Parts SHALL be written in order of their first occurrence. When a part's write fails, the
server SHALL send nothing more and fail with a tool error naming the parts already changed
and the part that failed, by `uid` and first occurrence; earlier parts SHALL NOT be undone.

#### Scenario: Title across a split series
- **WHEN** a series split in Calendar into two parts of one recurrence set is changed with
  `update_all` and a new `title`
- **THEN** iCloud sees one `PUT` with `If-Match` per part, every occurrence of both parts
  `calendar_events` returns has the new title, and the result's `parts` has `found` 2 and
  `changed` 2

#### Scenario: Parts found without property filters
- **WHEN** the same series is changed with `update_all` and a new title through its second
  part's id
- **THEN** iCloud sees `calendar-query` `REPORT`s with a `time-range`, one open-ended from
  the second part's last occurrence and one from 366 days before today to its first
  occurrence, none with a `prop-filter`, `param-filter` or `collation`, and both parts get
  the new title

#### Scenario: Part that ended more than a year ago
- **WHEN** a set's first part ended two years ago, its second part runs from then on, and it
  is changed with `update_all` and a new title through the second part's id
- **THEN** no earlier-parts listing is sent, only the second part is written, `parts.found`
  is 1 and `parts.searched_from` is 366 days before today

#### Scenario: From a date never looks back
- **WHEN** a set with parts starting 2027-01-04 and 2027-03-01 is changed with `update_from`
  and `effective` `2027-03-08` through the first part's id
- **THEN** the only part listing is an open-ended `time-range` from 2027-03-08, and the first
  part is not written

#### Scenario: Pattern change on a split series
- **WHEN** the same series is changed with `update_all` and a new `start`
- **THEN** the call fails with `this series was split; change it from a date (replace_from /
  update_from)` and no write request is sent

#### Scenario: Replace on a split series
- **WHEN** the same series is changed with `replace_all`
- **THEN** the call fails with the same error and no write request is sent

#### Scenario: From a date with a later part
- **WHEN** a set has parts starting 2027-01-04 and 2027-03-01 and is changed with
  `update_from`, `effective` `2027-02-01` and a new `location`
- **THEN** the first part is split at 2027-02-01 and the 2027-03-01 part gets the new
  location with one `PUT` with `If-Match`

#### Scenario: Pattern change from a date with a later part
- **WHEN** the same set is changed with `update_from`, `effective` `2027-02-01` and a new
  `recurrence`
- **THEN** the call fails with `this series was split on 2027-03-01; change its pattern from
  that date or later` and no write request is sent

#### Scenario: From the start of a later part
- **WHEN** the same set is changed with `update_from`, `effective` `2027-03-01` and a new
  `recurrence`
- **THEN** the 2027-03-01 part is changed in place with one `PUT` with `If-Match`, no new
  resource is created, and the first part is not written

#### Scenario: Later part fails
- **WHEN** a two-part set is changed with `update_all` and a new title and the second part's
  `PUT` is answered 412
- **THEN** the call fails with an error naming the first part as changed and the second as
  failed, and nothing more is sent

#### Scenario: Too many parts
- **WHEN** a recurrence set has 11 parts
- **THEN** the call fails with `this series has more than 10 parts; change it in Calendar`
  and no write request is sent

### Requirement: Series edit writes and rollback
An `_all` mode SHALL send one `PUT` with `If-Match` (the ETag of the fresh read) per part it
changes. A `_from` mode that splits a part SHALL first send a `PUT` with `If-None-Match: *`
for the new resource and then a `PUT` with `If-Match` (the ETag of the fresh read) for the
part being cut. When that second `PUT` is answered with HTTP 412, 404 or another 4xx, the
server SHALL read the new resource once (one single-href `calendar-multiget`) and send
`DELETE` with `If-Match` of that ETag, and then fail with the second `PUT`'s error (for 412:
`the event changed in Calendar; read it again`). When the second `PUT` gets no answer, is
throttled, or the rollback fails, the server SHALL send nothing more and fail with a tool
error saying the series may now be stored twice from the effective date and naming the new
series' `id`. Later parts SHALL be written only after the split succeeded. No request SHALL
be retried, and a call SHALL send at most one write request per part plus, for a split, one
for the new resource and one rollback `DELETE`.

#### Scenario: Series changed meanwhile
- **WHEN** the series' resource changes in Calendar between the fresh read and the second
  `PUT` of an `update_from`
- **THEN** iCloud answers 412, the new resource is deleted again, the call fails with `the
  event changed in Calendar; read it again`, and the calendar holds exactly what it held
  before the call plus the other change

#### Scenario: New resource not created
- **WHEN** the first `PUT` of a `replace_from` is refused
- **THEN** no second `PUT` is sent and the series is unchanged

#### Scenario: Second write unconfirmed
- **WHEN** the second `PUT` gets no answer
- **THEN** no further request is sent and the error names the new series' `id` and says the
  series may be stored twice

### Requirement: Series edit result and preview
The result SHALL include `mode`, `preview`, and `id`, `uid`, `calendar`, `calendar_id`,
`start`, `end`, `timezone`, `all_day` and `recurring` of the first occurrence of the changed
part in the formats of `calendar_event` (readable by `calendar_event` at once unless it is a
preview); `occurrences`, the number of occurrences that follow the new definition in every
changed part after deleted ones are removed, counted to the last one or, for an endless
series, through 366 days after the first changed occurrence, with `occurrences_through` that
last day counted (null when the series ends); `exceptions` with the counts `kept`,
`discarded` and `unmatched` over every changed part; `unmatched`, one entry per unmatched
exception with `kind`, `original_start` and, for kind `changed`, `start`, `end` and `title`;
`parts` with the number of parts `found` (1 without a recurrence set), the number `changed`,
`split` (whether a part was split into two resources), and `searched_from`, the start of the earlier-parts window (366 days before the current date)
in the `_all` modes for a series with a recurrence set, otherwise null; with
`preview: true`, `requires_drop_unmatched`; and, when a part was split, `previous` with the
old series' `uid` and the start of its last occurrence, and `ids_changed_from`. With
`preview: true` the call SHALL read and check exactly as without it and return the same
result, and SHALL send no write request.

#### Scenario: Counts reported
- **WHEN** a series of 20 occurrences with three deleted occurrences after the date is
  changed with `update_from` at its eleventh occurrence
- **THEN** the result has `occurrences` 7, `occurrences_through` null and `exceptions.kept` 3

#### Scenario: Endless series
- **WHEN** a weekly series without an end is changed with `update_all`
- **THEN** `occurrences` counts the occurrences in the 366 days from its first occurrence and
  `occurrences_through` is the last of those days

#### Scenario: Preview changes nothing
- **WHEN** a caller runs `replace_all` with `preview: true` on a series with exceptions
- **THEN** the result reports the exceptions that would be discarded, iCloud sees only read
  requests, and the stored resource and its ETag are unchanged

### Requirement: Series edit input limits
`calendar_series_update` SHALL refuse, with a tool error naming the field and before any
CalDAV request, a `mode` other than the four values, a missing or non-ISO `effective` in the
`_from` modes, an `effective` in the `_all` modes, a `drop_unmatched` or `preview` that is not
a boolean, and every input `calendar_event_update` and `calendar_event_create` refuse for the
same fields.

#### Scenario: Unknown mode
- **WHEN** a caller passes `mode` `this_and_following`
- **THEN** the call fails with a tool error mentioning `mode` and iCloud sees no request

#### Scenario: Missing effective date
- **WHEN** a caller runs `replace_from` without `effective`
- **THEN** the call fails with a tool error mentioning `effective` and iCloud sees no request
