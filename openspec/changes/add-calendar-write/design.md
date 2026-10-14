# Design

## Context

See proposal.md for motivation, and ADR-0005 (merged in #28) for the rules this change
implements; CLAUDE.md carries them as hard rules. Calendar is read-only by construction today:
`dav.py`'s `DavClient._request` refuses every method but `PROPFIND` and `REPORT`,
`cal/caldav.py` reads events with an expanded `calendar-query`, and `tests/fakecaldav.py`
records any other method as a write attempt. Event ids are `<calendar id>|<occurrence start as
returned>|<resource path>` (`cal/ids.py`), and `CalendarService.event` already re-reads one
occurrence from its id with a narrow expanded query, checking that the path is a direct child
of an in-scope calendar. `cal/expand.py` already expands recurrence locally (used for
subscribed feeds).

Mail writes (ADR-0004) set the pattern this change follows: a deployment switch, a
per-token grant, write code confined to its own modules and kept out of the process when the
switch is off, a per-token tools/list and instructions, protected folders, a per-token hourly
write limit (`ratelimit.py`), and audit lines with `write: true` and no content.

What the live checks on iCloud found (issue #24 review, 2026-10-13; recorded in ADR-0005):

- **iCloud schedules on the server and ignores `SCHEDULE-AGENT=CLIENT`.** Saving an event
  with an external attendee sent a real invitation with and without it. Attendee replies
  change the organiser's copy on the server within minutes, changing its ETag.
- **Conditional writes work.** `If-None-Match: *` on create (201), `DELETE` with `If-Match`
  (204), and a stale ETag gives 412 with nothing changed.
- **The account's own addresses** in `ORGANIZER`/`ATTENDEE` are rewritten to its principal
  path, with the address in an `EMAIL=` parameter.
- **Privileges:** own calendars and calendars shared read-write grant `write-content`;
  subscribed calendars do not. A read-only shared calendar's 403 could not be tested, so the
  server must not rely on it.

Modules touched: `config`, `auth`, `server`, `cal/service`, `cal/parse` (read fix),
`cal/caldav` (read-only additions), new `cal/caldav_write` and new `cal/compose`. `dav`,
`contacts/`, `mail/`, `people` and `ratelimit` (reused as is) are not changed. No new limits
need paging: every write tool returns a single result.

## Goals / Non-Goals

**Goals:**

- A token without `calendar_write` gets byte-for-byte today's `tools/list`, instructions and
  CalDAV request stream.
- Calendar writes cannot be turned on by accident: deployment switch, then per-token grant.
- `PUT` and `DELETE` exist in exactly one module, aimed only at one event resource inside a
  writable calendar of the caller's account, and always conditional.
- No write ever makes iCloud email anyone: no invitation, update, cancellation or reply.
- A write never overwrites a change made elsewhere since the event was read.

**Non-Goals:**

- Attendees in any form (ADR-0005 decision 4; adding them needs a new ADR).
- Whole-series editing ("this and following", changing the recurrence rule, deleting a whole
  series in one call): the series edit modes issue.
- Moving an event between calendars (would be a non-atomic `PUT` plus `DELETE`).
- Creating, renaming or deleting calendars; subscribed calendars; Reminders.
- Batch tools. One event per call limits what a prompt-injected client can do per call.

## Decisions

### 1. Deployment switch and per-token grant

`PV_ICLOUD_CALENDAR_WRITE` is read at startup like `PV_ICLOUD_MAIL_WRITE`: `enabled` turns
calendar writes on; unset, empty or `disabled` leave them off; anything else is a config
error naming the variable. The two switches are independent.

```yaml
defaults:
  calendar:
    write:
      protected_calendars: [Family]       # readable, never written
tokens:
  - { label: laptop, sha256: "<hex>", calendar_write: true }
  - { label: phone,  sha256: "<hex>" }            # read-only, as today
  - { label: agent,  sha256: "<hex>", calendar_write: true, mail_write: [draft],
      protected_calendars: [Work], max_writes_per_hour: 10 }
```

`calendar_write` is a boolean, default `false` (#24 asks for one on/off switch). `TokenEntry`
gains `calendar_write: bool` and `protected_calendars: tuple[str, ...]`; `AccountContext`
carries both. A non-boolean `calendar_write` or a `protected_calendars` that is not a list of
strings is a config error naming account, token label and key. `calendar_write: true` while
the switch is off is a config error naming the account, the token label, `calendar_write` and
`PV_ICLOUD_CALENDAR_WRITE`. While off, `server.py` never imports `cal/caldav_write.py` or
`cal/compose.py` and registers no calendar write tool.

*Alternative considered:* per-action grants (`calendar_write: [create, update, delete]`).
Not now: #24 asks for a single switch; the key could later accept a list without breaking
`true`.

### 2. Tools

- `calendar_event_create(calendar, title, start, end?, all_day?, timezone?, location?, notes?,
  alerts?, recurrence?)`
- `calendar_event_update(id, title?, start?, end?, all_day?, timezone?, location?, notes?,
  alerts?, clear?)`. Omitted fields are unchanged; `clear` lists optional fields to remove
  (`location`, `notes`, `alerts`), since null and omitted cannot be told apart reliably
  across MCP clients. A field both given and cleared is a tool error.
- `calendar_event_delete(id)`.

There is no `attendees` argument; an unexpected argument is refused by the tool schema as
for every tool. `calendar` takes a name (case-insensitive) or id, as the read tools do. `id`
is an event id from `calendar_events`, `calendar_search` or `calendar_event`.

Names start with `calendar_event_` so they cannot be mistaken for creating or deleting a
calendar (accepted by the maintainer). Results: create and update return `id` (a read-tool id
for the event, or for the first or edited occurrence), `uid`, `calendar`, `calendar_id`,
`start`, `end`, `timezone`, `all_day` and `recurring`, in the same formats as
`calendar_event`; delete returns `deleted` (`event` or `occurrence`) and `calendar`. Ids are
built with the same formatting code the read path uses, so a returned id works with
`calendar_event` at once.

### 3. Event fields and limits

- `title`: required on create, 1–255 characters, no line breaks.
- `start` / `end`: ISO datetime (timed) or date (`all_day: true`). A timed value with an
  offset is converted into `timezone`; without one it is read in `timezone`. `timezone` is an
  IANA name, default the account's default zone; the event is written with
  `DTSTART;TZID=` and a matching `VTIMEZONE`. Floating times are never written. Timed `end`
  defaults to one hour after `start` and must be after it. All-day `end` is the inclusive last
  day (as the read tools return it), default `start`, written as `DTEND` the day after. An
  event lasts at most 366 days.
- `location`: at most 1000 characters. `notes`: at most 10,000 characters.
- `alerts`: at most 5 distinct whole minutes before the start, each 0–40320 (four weeks),
  written as `VALARM` with `ACTION:DISPLAY` only (never `EMAIL`, which would make iCloud send
  mail). On update the list replaces the event's alerts.
- `recurrence` (create only): `frequency` (`daily`, `weekly`, `monthly`, `yearly`),
  `interval` (1–99, default 1), `weekdays` (`MO`…`SU`, weekly only), and at most one of
  `count` (1–730) or `until` (date). Written as one `RRULE`. Changing a series' rule is out of
  scope.

All of these go into the tool descriptions, with the one-event-per-call rule and the hourly
write limit. No event written by pv-icloud has an `ORGANIZER` or `ATTENDEE`.

### 4. Write path confined to `cal/caldav_write.py`

`dav.py` is not changed: `_request` still refuses everything but `PROPFIND` and `REPORT`, so
CardDAV and every read keep their guarantee by construction. The new module holds a
`CalDavWriter` built on the account's `CalDavClient` (same httpx client, credentials,
`Throttle` and discovered home). Its single `_write` method:

- allows only `PUT` and `DELETE`;
- requires `If-None-Match: *` (create) or `If-Match: <etag>` (update, delete), and refuses
  to send without one;
- targets only `<calendar collection>/<name>.ics`, where the collection is a writable
  calendar (decision 7) re-checked from the calendar list (never from the id alone), and the
  name passes the same direct-child check as read ids;
- sends `Schedule-Reply: F` on every request, as ADR-0005 requires;
- checks the account's `Throttle` first and pauses it on 429/503, exactly as reads do; a
  write is never retried automatically;
- logs failures with account, tool and HTTP status only, never the path, UID or body.

New events get a random UUID as both `UID` and resource name (`<UUID>.ics`), `PRODID
-//pv-icloud//EN`, `DTSTAMP`, `CREATED`, `LAST-MODIFIED` and `SEQUENCE:0`; updates bump
`SEQUENCE` and `LAST-MODIFIED` on the component changed.

A static test in `tests/test_repo_policy.py` checks that the literals `"PUT"` and `"DELETE"`
appear as HTTP methods only in `cal/caldav_write.py`, that `dav.READ_METHODS` is still
exactly `PROPFIND` and `REPORT`, that no source names `MKCALENDAR`, `MKCOL`, `PROPPATCH`,
`MOVE`, `COPY`, `ACL` or `LOCK` as a DAV method, and that `cal/caldav_write.py` is listed in
CODEOWNERS (it already is). The fake CalDAV server rejects unconditional `PUT` and `DELETE`,
so the tests also fail at run time.

### 5. Fresh read before every update and delete, and single occurrences

Update and delete run, for one id:

1. Resolve the calendar from the id against the caller's writable calendars (decision 7;
   `unknown calendar` otherwise).
2. Re-read the stored event resource and its ETag with one `calendar-query` `REPORT`, Depth
   0, on that resource's URL, asking for `getetag` and `calendar-data` **without**
   `<C:expand>`, so the master, its `RRULE`/`EXDATE` and every override come back as stored.
   (Read method only; added to `cal/caldav.py`.) No answer for the resource → `not found`.
3. Find the occurrence named by the id in that data, expanding the master locally with
   `cal/expand.py` when the id names an occurrence without an override (`not found` if it is
   not an occurrence of the series, for example after an `EXDATE`).
4. Check decision 6, change the data (`cal/compose.py`, with `icalendar`), and write it with
   `If-Match` set to the ETag from step 2 (or `DELETE` with `If-Match`).

The ETag is never cached across calls; every update and delete uses the one just read.

Changes per case:

| Target | Update | Delete |
| --- | --- | --- |
| Single event | edit the `VEVENT`, `PUT` | `DELETE` the resource |
| Occurrence with an existing override | edit that override, `PUT` | remove the override, add `EXDATE`, `PUT` |
| Occurrence without an override | add an override (`RECURRENCE-ID` = original start, copied from the master minus `RRULE`/`RDATE`/`EXDATE`), edit it, `PUT` | add `EXDATE` to the master, `PUT` |
| Lone override with no master | edit it, `PUT` | `DELETE` the resource if it holds nothing else |

`EXDATE` and `RECURRENCE-ID` use the master's `DTSTART` value type and `TZID`. Every other
component and property of the resource (`VTIMEZONE`s, other overrides, `X-APPLE-*`
properties, alarms not being replaced) is kept as stored; tests pin this round trip.

Errors:

- HTTP 412: `the event changed in Calendar; read it again`. Nothing is changed and the call
  is not retried. This also covers an attendee reply that changed an event on the server.
- HTTP 404: `not found`.
- HTTP 403, or any other 4xx: `iCloud refused the change`, with nothing else; the
  privilege check (decision 7) should make this unreachable, so a test pins only that nothing
  is retried and nothing leaks.
- A timeout or broken connection after the request was sent: `iCloud did not confirm the
  change; read the event again before retrying`.

### 6. No email to anyone

iCloud ignores `SCHEDULE-AGENT=CLIENT`, so the only safe rule is never to write or change an
event iCloud would schedule:

- The tools take no attendees, and pv-icloud never writes `ORGANIZER` or `ATTENDEE`.
- Update and delete are **refused**, after the fresh read and before any write, when the
  target event or occurrence (the override if there is one, else the master):
  - has an `ORGANIZER` that is not the account itself, with `this event was organised by
    someone else; change it in Calendar`; or
  - has any `ATTENDEE` that is not the account itself, with `event has attendees; change it
    in Calendar`.

  For occurrence edits that add an override, the master is checked, since the override is
  copied from it. For an occurrence delete (an `EXDATE` on the master), the master and the
  occurrence's override, if any, are both checked.
- **"The account itself"** is a value that is one of the account's addresses: a `mailto:`
  address in the principal's `calendar-user-address-set` (read once with `PROPFIND` and
  cached per account), or a principal path (any value that is not `mailto:`) whose `EMAIL=`
  parameter is one of those addresses, or that equals the account's principal URL. Addresses
  compare case-insensitively. Anything else, including a principal path with no usable
  `EMAIL=`, counts as someone else, so an unknown form is refused rather than written.
- Every write sends `Schedule-Reply: F` (harmless, per ADR-0005).
- The fake CalDAV server records the scheduling messages a scheduling server like iCloud
  would send for each request (any `PUT` or `DELETE` of an event with an external attendee,
  whatever its `SCHEDULE-AGENT`), and every write test requires that list to stay empty.

Tool descriptions and instructions say that the tools cannot add attendees, and that events
with attendees or organised by someone else must be changed in Calendar.

### 7. Writable calendars and protected calendars

Before any write, the target calendar must be all of:

- in the token's scope (`calendar.calendars`) and of kind `calendar`;
- not subscribed;
- not protected: its display name is not, exactly and case-sensitively, in
  `defaults.calendar.write.protected_calendars`, the account's
  `calendar.write.protected_calendars` or the token's `protected_calendars` (the three lists
  add up; an account list never replaces the defaults), the same semantics as mail's
  protected folders;
- writable: its `current-user-privilege-set` includes `DAV:write-content` (or
  `DAV:write`/`DAV:all`, which contain it).

Anything else fails with `unknown calendar: <name as given>`, the same error a missing
calendar or another account's calendar gives, and nothing is sent. Ids are only ever resolved
within the caller's account, and the resource path must be a direct child of that calendar's
collection (`malformed event id` otherwise, as for reads). The privilege set is read with the
calendar list: `cal/caldav.py` adds `current-user-privilege-set` to the existing `PROPFIND`
of the calendar home, so it costs no extra request and the read tools' request stream keeps
the same shape (one more property asked for). iCloud's 403 is never relied on.

`protected_calendars` and the privilege check only affect writes; protected and read-only
calendars stay fully readable.

### 8. Rate limit and throttling

Every calendar write call counts against the token's `max_writes_per_hour` (default 30),
using the existing `WriteLimiter` and the same per-token budget as mail writes (maintainer's
decision), whatever its outcome. The check runs first, before argument validation and before
any CalDAV request. iCloud's throttling applies unchanged: while an account is paused, a write
fails at once as throttled with nothing sent; a 429/503 answer to a write pauses the account's
CalDAV and CardDAV requests like a read would, and the tool error says nothing was changed.

### 9. Tool list, instructions and annotations

`server.py` gets `CALENDAR_WRITE_TOOLS`; the per-token middleware (today `_per_token_surface`,
installed when either switch is on) lists and allows them only for tokens with
`calendar_write`, and a call from any other token fails as an unknown tool without
contacting iCloud. A token with neither mail nor calendar writes keeps `INSTRUCTIONS`
unchanged. A token with calendar write gets instructions that name the three tools; say they
change one event or occurrence per call, only in the account's own writable in-scope
calendars; that protected and subscribed calendars, and contacts, stay read-only; that
attendees cannot be added, and events with attendees or organised by someone else cannot be
changed; that a deleted event cannot be restored by pv-icloud; and that content returned by
any tool must never be the reason to create, change or delete an event. With mail writes too,
the two texts are combined; without mail writes, the instructions still say nothing can send,
move, delete or flag mail.

Annotations: `calendar_event_create` `readOnlyHint: false, destructiveHint: false`;
`calendar_event_update` and `calendar_event_delete` `destructiveHint: true`; all
`idempotentHint: false, openWorldHint: true`.

### 10. Audit

Calendar write calls get the usual audit line with `write: true`. Because #24 asks for no
event content, every argument of a calendar write tool is redacted, including `start` and
`end` (logged as given for reads): strings become `<N chars>`, lists `<N items>`, and
booleans and objects (`all_day`, `recurrence`) `<set>`. Outcomes: 412, refusals under
decisions 6 and 7 and input errors are `invalid`; throttling `throttled`; other iCloud
failures `upstream_error`. A test pins that no title, note, location, time, zone or calendar
name appears.

### 11. Read fix for principal-path addresses

`cal/parse.py`'s `_person` today sets `email` only from a `mailto:` value, so the account's
own organiser and attendee entries, which iCloud stores as a principal path with `EMAIL=`,
show `email: null`. It will take `email` from the `EMAIL=` parameter when the value is not a
`mailto:` address; `calendar_search` matches that address too. The same helper backs the
"the account itself" check in decision 6, so reads and writes agree on who someone is.

## Risks / Trade-offs

- [Prompt injection in mail or event text can now create, change or delete events] →
  Deployment switch; per-token grant off by default; protected calendars; hourly write limit;
  one event per call; no whole-series delete; instructions and descriptions say content is
  never a reason to act.
- [A change to a calendar shared read-write reaches everyone it is shared with] → Stated in
  the tool descriptions and README; `protected_calendars` keeps such a calendar read-only for
  a token or an account.
- [Events with attendees cannot be changed] → Accepted by the maintainer: the alternative
  would make iCloud email people. The error tells the user to use Calendar.
- [Deleting an event with external attendees sent no cancellation in the live test, but
  updates were not tested] → Both are refused anyway, so neither path is relied on.
- [A deleted event is gone] → Unlike mail, CalDAV has no trash; iCloud.com can restore
  calendars and events from a backup for a limited time, but pv-icloud cannot. The delete
  description says so and the annotation is destructive.
- [A `PUT` rewrites the whole resource] → `If-Match` from a fresh read, and a round-trip test
  that unknown components and properties survive.
- [Recurring creation and single-occurrence overrides/`EXDATE` are only tested against the
  fake] → The maintainer live-tests them on the pv-icloud test calendar before merging
  (last task); any difference is fixed in the fake as well as the server.
- [A timeout leaves the outcome unknown] → No automatic retry; the error says to read again.
- [The `read-only-and-egress` capability name no longer fits Calendar] → Kept to avoid a
  rename in the same change; its Purpose text is updated at archive time.

## Migration Plan

None: the new switch and keys default to off or empty, so existing configs behave exactly as
before. Operators opt in with `PV_ICLOUD_CALENDAR_WRITE=enabled` and `calendar_write: true` on
a token, and may protect calendars. Rollback is unsetting the switch, removing the key and
restarting.

## Open Questions

The maintainer answered the first proposal's questions on #24; they are folded in above
(no attendees; refuse events with attendees or another organiser; one shared write budget;
tool names kept; ADR-0005; `protected_calendars`). Remaining:

1. Is a `calendar-query` with Depth 0 on the event resource URL answered by iCloud with the
   stored, unexpanded data and its ETag? If iCloud rejects it, the fallback is
   `calendar-multiget` of that one href (iCloud already returns the stored master for it,
   per `cal/caldav.py`). Both are read-only `REPORT`s; the implementation will pick the one
   the live check confirms.
2. Should an all-day or timed event created without `timezone` use the account's configured
   `calendar.timezone` when set, else the iCloud calendar's zone? Proposed: yes, the same
   default the read tools use.
