## MODIFIED Requirements

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
