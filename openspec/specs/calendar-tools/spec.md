# calendar-tools Specification

## Purpose
The calendar tools: `calendar_list`, `calendar_events`, `calendar_search` and `calendar_event`,
with calendar scope, time-zone and all-day rules, date ranges, event shapes, ids and limits.

## Requirements

### Requirement: Calendars listed from iCloud
`calendar_list` SHALL return the account's in-scope calendars from its iCloud CalDAV home: event
calendars (collections that support `VEVENT`, `kind` `calendar`) and subscribed webcal calendars
(`kind` `subscribed`). Reminders lists, scheduling inbox and outbox and other collections SHALL
be left out. Each calendar SHALL have `id` (last path segment of its URL), `name` (`(unnamed)`
when it has none), `color` (`#RRGGBB`), `kind`, `shared_with_me`, `timezone` (its default zone
name, or null) and `events_available`; subscribed calendars SHALL also have `source_host`, the
host of the feed URL, and never the URL itself. Calendars SHALL be sorted with `kind`
`calendar` first, then by case-insensitive name. The result SHALL include `default_timezone`.
The calendar list SHALL be cached for 300 seconds per account.

#### Scenario: Listing calendars
- **WHEN** an account has `Home`, a shared `Work`, an excluded `Banking`, a Reminders list and a
  subscribed `TripIt`
- **THEN** the list is `Home`, `Work`, `TripIt`; `Work` has `shared_with_me` true; `TripIt` has
  `kind` `subscribed` and `source_host` but no feed URL

#### Scenario: Listing is cached
- **WHEN** `calendar_list` is called twice within 300 seconds
- **THEN** iCloud is asked for the list once

### Requirement: Calendar scope
A calendar SHALL be in scope when its display name is matched by `calendar.calendars.include`
(`"*"` or a list of names or globs, default `"*"`) and by no glob in
`calendar.calendars.exclude`, case-sensitively. The scope SHALL apply to subscribed calendars too.
Calendars out of scope SHALL never be listed, queried or resolved from an id.

#### Scenario: Excluded calendar is never queried
- **WHEN** `Banking` is excluded and a caller lists events
- **THEN** no request reads events from `Banking`

### Requirement: Default time zone
The account's default time zone SHALL be `calendar.timezone` when set; otherwise the default zone
of the account's own (not shared) iCloud event calendars, preferring the calendar with id `home`;
otherwise UTC. Date-only bounds and times without an offset SHALL be read in this zone.

#### Scenario: Zone from iCloud
- **WHEN** `calendar.timezone` is unset and the `home` calendar's zone is `America/Chicago`
- **THEN** `default_timezone` and each result's `range.timezone` are `America/Chicago`

#### Scenario: Zone from config
- **WHEN** `calendar.timezone` is `Europe/London`
- **THEN** `range.timezone` is `Europe/London`

### Requirement: Date range rules
`start` and `end` SHALL accept an ISO date (`YYYY-MM-DD`) or datetime. A date-only `start` SHALL
mean midnight at the start of that day; a date-only `end` SHALL mean midnight at the end of that
day, so the day is included; a datetime `end` SHALL be exclusive; values without an offset SHALL
be in the default time zone. An `end` not after `start`, a range longer than the tool's maximum,
or any other format SHALL be a tool error.

#### Scenario: One whole day
- **WHEN** `start` and `end` are both the same date
- **THEN** the events of that whole day are returned

#### Scenario: Exclusive datetime end
- **WHEN** an event starts exactly at a datetime `end`
- **THEN** it is not returned

#### Scenario: End before start
- **WHEN** `end` is before `start`
- **THEN** the call fails with `end must be after start`

### Requirement: calendar_events range and limits
`calendar_events` SHALL default to today (midnight in the default zone) through 7 days later.
With only `start`, the range SHALL be 7 days from `start`; with only `end`, the 7 days before the
end of `end`. The range SHALL be at most `calendar.max_range_days` (default 90). `limit` SHALL
default to 100 with a maximum of 200.

#### Scenario: Default range
- **WHEN** a caller lists events without arguments
- **THEN** `range.start` is today's midnight in the default zone and events of the next 7 days
  are returned

#### Scenario: Range too long
- **WHEN** a caller asks for 91 days with the default configuration
- **THEN** the call fails with a tool error mentioning `90 days`

### Requirement: calendar_search range and limits
`calendar_search` SHALL require a non-blank `query`. Its default range SHALL be centred on
today: from today minus `calendar.max_search_days` ÷ 2 (rounded down) days to today plus the
rest. With only `start` or only `end`, the range SHALL span `calendar.max_search_days` from that
bound. The range SHALL be at most `calendar.max_search_days` (default 730). `limit` SHALL default
to 50 with a maximum of 200.

#### Scenario: Default search range
- **WHEN** a caller searches with the default configuration and no dates
- **THEN** the range starts 365 days before today and covers 730 days

#### Scenario: Blank query
- **WHEN** `query` is only spaces
- **THEN** the call fails with a tool error mentioning `query`

#### Scenario: Upcoming events only
- **WHEN** a caller searches with `start` set to a date
- **THEN** only matching events from that date on are returned

### Requirement: Choosing calendars
Without `calendars`, the event tools SHALL read every in-scope calendar whose events are
available. `calendars` SHALL take calendar names (case-insensitive) or ids. A name or id that
matches no in-scope calendar SHALL be a tool error `unknown calendar: <name>`, the same for an
excluded calendar as for a missing one. Naming a subscribed calendar while subscriptions are
turned off SHALL be a tool error saying so. The result SHALL list the names read in
`calendars_searched`.

#### Scenario: By name or id
- **WHEN** a caller passes `calendars` `["work"]` or the `Work` calendar's id
- **THEN** only events from `Work` are returned

#### Scenario: Excluded calendar named
- **WHEN** a caller passes `calendars` `["Banking"]` and `Banking` is excluded
- **THEN** the call fails with `unknown calendar: Banking`

### Requirement: Matching and order
An event SHALL be returned when it starts before the range end and either ends after the range
start or has zero length and starts inside the range. When `query` is given, every word of it
SHALL appear, ignoring case and accents, in the event's title, location, full notes, calendar
name, or organizer and attendee names and email addresses. Events SHALL be sorted by start time,
then title, calendar id and UID. Each result SHALL include `events`, `total`, `offset`,
`next_offset`, `range` (`start`, `end`, `timezone`) and `calendars_searched`.

#### Scenario: Words across fields
- **WHEN** an event's location is `Heathrow` and its notes contain `Agenda`
- **THEN** `query` `AGENDA lhr` finds it (title `Flight to LHR`), and `heathrow dentist` does not

#### Scenario: People and calendar name
- **WHEN** an event's organizer is `Doe, Jane` and an attendee is `carol@example.com`
- **THEN** searching `jane` or `carol@example.com` finds it, and searching a calendar's name finds
  that calendar's events

### Requirement: Event fields
Each event SHALL have `id`, `title` (`(no title)` when empty), `calendar`, `calendar_id`,
`all_day`, `start`, `end`, `location`, `notes`, `recurring` and `uid`, and when present `url`,
`status` (lower case), `organizer` and `attendees` (each `name`, `email` and, when known,
`status` lower case). A person's `email` SHALL be the address of a `mailto:` value; for any
other value (iCloud stores the account's own address as its principal path) it SHALL be the
value's `EMAIL=` parameter, and null when there is none. Notes longer than 1000 characters
SHALL be cut, with `notes_truncated` true; at most 50 attendees SHALL be listed, with
`attendees_total` giving the full count when there are more. Events without a start SHALL be
skipped; an event without an end SHALL use its duration, else last one day (date) or no time
(datetime). One unparseable resource or event SHALL NOT hide the rest of the calendar.

#### Scenario: Long notes and many attendees
- **WHEN** an event has 1500 characters of notes and 60 attendees
- **THEN** `calendar_events` returns 1000 characters with `notes_truncated` true, 50 attendees
  and `attendees_total` 60

#### Scenario: Account's own address as a principal path
- **WHEN** an event's `ORGANIZER` is a principal path with `EMAIL=me@example.com`
- **THEN** the event's `organizer` has `email` `me@example.com`, and `calendar_search` for
  `me@example.com` finds it

### Requirement: Time zones and all-day events
A timed event's `start` and `end` SHALL be ISO 8601 datetimes in the event's own time zone with
its offset, and `timezone` SHALL be the zone name (`UTC` for UTC times). A floating time (no
zone) SHALL be written without an offset, with `timezone` null and a `timezone_note` saying it is
floating; for range filtering it SHALL be read in the default zone. An all-day event SHALL have
`all_day` true, date-only `start` and `end`, an inclusive last day, and no `timezone`.

#### Scenario: Event in another zone
- **WHEN** an event is at 08:00 `Europe/London` and the default zone is `America/Chicago`
- **THEN** its `start` carries the London offset and `timezone` is `Europe/London`

#### Scenario: Two-day all-day event
- **WHEN** an all-day event has DTSTART day 3 and DTEND day 5
- **THEN** `start` is day 3 and `end` is day 4

#### Scenario: Floating time
- **WHEN** an event's start has no zone
- **THEN** `start` has no offset, `timezone` is null and `timezone_note` mentions floating time

### Requirement: Recurring events from iCloud
For iCloud calendars, the server SHALL read events with one CalDAV `calendar-query` per calendar
with `<C:expand>` over the range, so iCloud expands recurring events and each occurrence is
returned separately; calendars SHALL be read in parallel (up to 8 at a time). An occurrence SHALL
be `recurring` when it has a `RECURRENCE-ID` or its UID appears more than once in its resource.

#### Scenario: Weekly series over a long range
- **WHEN** a weekly standup has occurrences on days 1, 8 and 15 and a caller searches `standup`
- **THEN** three separate events are returned, each with `recurring` true

### Requirement: Partial failures
When some calendars fail, the result SHALL contain the events from the others and an `errors`
list with one entry per failing calendar: `calendar` (its name) and `error`. When every calendar read fails, the call
SHALL fail, as throttled when one of the failures was throttling.

#### Scenario: One calendar fails
- **WHEN** iCloud answers HTTP 500 for `Work`
- **THEN** the other calendars' events are returned and `errors` names `Work`

#### Scenario: All calendars fail
- **WHEN** every calendar read fails and no cached feed copy exists
- **THEN** the call fails

### Requirement: Event ids
An event id SHALL be the URL-safe base64 encoding, without padding, of
`<calendar id>|<occurrence start as returned>|<reference>`, where the reference is the CalDAV
resource path (starting with `/`) or `uid:<UID>` for a subscribed feed. An id that does not
decode to that shape, or whose start is not an ISO date or datetime, SHALL be a tool error
`malformed event id`. For an iCloud calendar the path MUST be a direct child of the calendar's
own collection (no `/`, `.` or `..` in the name, after percent-decoding); otherwise the id SHALL
be rejected as malformed.

#### Scenario: Id pointing into another calendar
- **WHEN** an id names calendar `home` but its path is inside another calendar, contains `..` or
  an encoded `/`, or is the collection itself
- **THEN** `calendar_event` fails with `malformed event id`

#### Scenario: Id naming an excluded calendar
- **WHEN** an id names the excluded calendar `BANK`
- **THEN** `calendar_event` fails with `unknown calendar`

### Requirement: calendar_event
`calendar_event` SHALL re-read the single occurrence named by the id with a narrow expanded
`calendar-query` covering two days either side of its start (never `calendar-multiget`), or from
the feed for a subscribed calendar, and return the occurrence with the same start. It SHALL
return the event's fields with every attendee, without `attendees_total`, and the full notes paged
by characters with `notes_offset`, `notes_total_chars` and `next_offset` (`notes` null when the
event has none). An occurrence that no longer exists SHALL be a tool error saying it was not
found.

#### Scenario: Full notes in pages
- **WHEN** an event has 1500 characters of notes and a caller reads it with `max_chars` 600
- **THEN** three pages return the full notes

#### Scenario: The right occurrence
- **WHEN** a caller passes the id of the second occurrence of a weekly series
- **THEN** the returned `start` is that occurrence's start, not the first one's

#### Scenario: Event deleted
- **WHEN** the event's resource no longer contains it
- **THEN** the call fails with a tool error mentioning `not found`
