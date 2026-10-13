# Design

## Context

See proposal.md for motivation. Calendar is read-only by construction today: `dav.py`'s
`DavClient._request` refuses every method but `PROPFIND` and `REPORT`, `cal/caldav.py`
reads events with an expanded `calendar-query`, and `tests/fakecaldav.py` records any other
method as a write attempt. Event ids are `<calendar id>|<occurrence start as returned>|<resource
path>` (`cal/ids.py`), and `CalendarService.event` already re-reads one occurrence from its
id with a narrow expanded query, checking that the path is a direct child of an in-scope
calendar.

Mail writes (ADR-0004) set the pattern this change follows: a deployment switch, a
per-token grant, write code confined to its own modules and kept out of the process when the
switch is off, a per-token tools/list and instructions, a per-token hourly write limit
(`ratelimit.py`), and audit lines with `write: true` and no content.

Two CalDAV behaviours make calendar writes riskier than they look:

- **Implicit scheduling (RFC 6638).** iCloud is a scheduling server. A `PUT` of an event the
  account organises, with attendees, makes iCloud send invitations, updates or
  cancellations; changing or deleting an event the account was invited to sends a reply to
  the organiser. #24 puts sending invitations and responses out of scope, so the server must
  never cause one.
- **Concurrent edits.** A `PUT` replaces the whole resource, including every override of a
  recurring series. Without a precondition, a change made in Apple's apps between our read
  and our write would be silently lost.

Modules touched: `config`, `auth`, `server`, `cal/service`, `cal/caldav` (read-only
additions), new `cal/caldav_write` and new `cal/compose`. `dav`, `contacts/`, `mail/`,
`people` and `ratelimit` (reused as is) are not changed.

Prerequisite, outside this change: the maintainer's own pull request adds ADR-0005 (opt-in
calendar write), updates CLAUDE.md's DAV hard rule, `.github/CODEOWNERS` (adding
`cal/caldav_write.py`), `openspec/config.yaml` and SECURITY.md. Implementation starts after it
merges and after open question 1 is answered.

## Goals / Non-Goals

**Goals:**

- A token without `calendar_write` gets byte-for-byte today's `tools/list`, instructions and
  CalDAV request stream.
- Calendar writes cannot be turned on by accident: deployment switch, then per-token grant.
- `PUT` and `DELETE` exist in exactly one module, aimed only at one event resource inside an
  in-scope event calendar of the caller's account, and always conditional.
- No write ever causes iCloud to send an invitation, update, cancellation or reply.
- A write never overwrites a change made elsewhere since the event was read.

**Non-Goals:**

- Whole-series editing ("this and following", changing the recurrence rule, deleting a whole
  series in one call): the series edit modes issue.
- Moving an event between calendars (would be a non-atomic `PUT` plus `DELETE`).
- Creating, renaming or deleting calendars; subscribed calendars; Reminders.
- Inviting attendees or answering invitations.
- Batch tools. One event per call limits what a prompt-injected client can do per call.

## Decisions

### 1. Deployment switch and per-token grant

`PV_ICLOUD_CALENDAR_WRITE` is read at startup like `PV_ICLOUD_MAIL_WRITE`: `enabled` turns
calendar writes on; unset, empty or `disabled` leave them off; anything else is a config
error naming the variable. The two switches are independent.

```yaml
tokens:
  - { label: laptop, sha256: "<hex>", calendar_write: true }
  - { label: phone,  sha256: "<hex>" }            # read-only, as today
  - { label: agent,  sha256: "<hex>", calendar_write: true, mail_write: [draft],
      max_writes_per_hour: 10 }
```

`calendar_write` is a boolean, default `false` (#24 asks for one on/off switch, not per-action
grants). `TokenEntry` gains `calendar_write: bool`; `AccountContext` carries it. A non-boolean
value is a config error naming account, token label and key. `calendar_write: true` while the
switch is off is a config error naming the account, the token label, `calendar_write` and
`PV_ICLOUD_CALENDAR_WRITE`. While off, `server.py` never imports `cal/caldav_write.py` or
`cal/compose.py` and registers no calendar write tool.

*Alternative considered:* per-action grants (`calendar_write: [create, update, delete]`).
Rejected for now because #24 asks for a single switch; the key could later accept a list
without breaking `true`.

### 2. Tools

- `calendar_event_create(calendar, title, start, end?, all_day?, timezone?, location?, notes?,
  attendees?, alerts?, recurrence?)`
- `calendar_event_update(id, title?, start?, end?, all_day?, timezone?, location?, notes?,
  attendees?, alerts?, clear?)`. Omitted fields are unchanged; `clear` lists optional fields
  to remove (`location`, `notes`, `attendees`, `alerts`), since null and omitted cannot be told
  apart reliably across MCP clients. A field both given and cleared is a tool error.
- `calendar_event_delete(id)`.

`calendar` takes a name (case-insensitive) or id, as the read tools do. `id` is an event id
from `calendar_events`, `calendar_search` or `calendar_event`.

Names start with `calendar_event_` so they cannot be mistaken for creating or deleting a
calendar. Results: create and update return `id` (a read-tool id for the event, or for the
first or edited occurrence), `uid`, `calendar`, `calendar_id`, `start`, `end`, `timezone`,
`all_day` and `recurring`, in the same formats as `calendar_event`; delete returns
`deleted` (`event` or `occurrence`) and `calendar`. Ids are built with the same formatting
code the read path uses, so a returned id works with `calendar_event` at once.

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
- `attendees`: at most 50, each `{email, name?}` with one bare address; on update the list
  replaces the old one, keeping the participation status of addresses that stay.
- `alerts`: at most 5 distinct whole minutes before the start, each 0–40320 (four weeks),
  written as `VALARM` with `ACTION:DISPLAY` only (never `EMAIL`, which would make iCloud send
  mail). On update the list replaces the event's alerts.
- `recurrence` (create only): `frequency` (`daily`, `weekly`, `monthly`, `yearly`),
  `interval` (1–99, default 1), `weekdays` (`MO`…`SU`, weekly only), and at most one of
  `count` (1–730) or `until` (date). Written as one `RRULE`. Changing a series' rule is out of
  scope.

All of these go into the tool descriptions, with the one-event-per-call rule and the hourly
write limit. Write tools return single results, so no paging is added.

### 4. Write path confined to `cal/caldav_write.py`

`dav.py` is not changed: `_request` still refuses everything but `PROPFIND` and `REPORT`, so
CardDAV and every read keep their guarantee by construction. The new module holds a
`CalDavWriter` built on the account's `CalDavClient` (same httpx client, credentials,
`Throttle` and discovered home). Its single `_write` method:

- allows only `PUT` and `DELETE`;
- requires `If-None-Match: *` (create) or `If-Match: <etag>` (update, delete), and refuses
  to send without one;
- targets only `<calendar collection>/<name>.ics`, where the collection is an in-scope event
  calendar of the caller's account, re-checked from the calendar list (never from the id
  alone), and the name passes the same direct-child check as read ids;
- sends `Schedule-Reply: F` on every request (RFC 6638 §8.1), as a second guard on top of
  decision 6;
- checks the account's `Throttle` first and pauses it on 429/503, exactly as reads do; a
  write is never retried automatically;
- logs failures with account, tool and HTTP status only, never the path, UID or body.

New events get a random UUID as both `UID` and resource name (`<UUID>.ics`), `PRODID
-//pv-icloud//EN`, `DTSTAMP`, `CREATED`, `LAST-MODIFIED` and `SEQUENCE:0`; updates bump
`SEQUENCE` and `LAST-MODIFIED` on the component changed.

A static test in `tests/test_repo_policy.py` checks that the literals `"PUT"` and `"DELETE"`
appear as HTTP methods only in `cal/caldav_write.py`, that `dav.READ_METHODS` is still
exactly `PROPFIND` and `REPORT`, and that `cal/caldav_write.py` is listed in CODEOWNERS. The
fake CalDAV server rejects unconditional `PUT` and `DELETE`, so the tests also fail at run
time.

*Alternatives considered:* adding `PUT`/`DELETE` to `READ_METHODS` or a flag on `DavClient`
(the read guarantee would then rest on call sites, not a module boundary); `PROPPATCH` or
`MKCALENDAR` (not needed for events; stay forbidden).

### 5. Reading before writing, and single occurrences

Update and delete run, for one id:

1. Resolve the calendar from the id against the caller's in-scope calendars (`unknown
   calendar` otherwise; subscribed calendars get `subscribed calendars are read-only`).
2. Re-read the occurrence with the narrow expanded `calendar-query` `calendar_event` already
   uses, to confirm it exists and learn its `RECURRENCE-ID` (`not found` otherwise).
3. Read the stored resource and its ETag with `calendar-multiget` for that one path,
   **without** `<C:expand>`, so the master, its `RRULE`/`EXDATE` and every override come back
   as stored. (Read methods only; added to `cal/caldav.py`.)
4. Check decision 6, change the data (`cal/compose.py`, with `icalendar`), and write it with
   `If-Match` (or `DELETE` with `If-Match`).

Changes per case:

| Target | Update | Delete |
| --- | --- | --- |
| Single event | edit the `VEVENT`, `PUT` | `DELETE` the resource |
| Occurrence with an existing override | edit that override, `PUT` | remove the override, add `EXDATE`, `PUT` |
| Occurrence without an override | add an override (`RECURRENCE-ID` = original start, copied from the master minus `RRULE`/`RDATE`/`EXDATE`), edit it, `PUT` | add `EXDATE` to the master, `PUT` |
| Lone override with no master | edit it, `PUT` | `DELETE` the resource if it holds nothing else |

`EXDATE` uses the master's `DTSTART` value type and `TZID`. Every other component and
property of the resource (`VTIMEZONE`s, other overrides, `X-APPLE-*` properties, alarms not
being replaced) is kept as stored; tests pin this round trip.

On HTTP 412 the call fails with `the event changed since it was read; read it again and
retry` and nothing is changed. On 404 it fails as not found; on 403 with `calendar <name>
does not accept changes` (for example a calendar shared read-only). On a timeout or broken
connection after the request was sent, it fails with `iCloud did not confirm the change; read
the event again before retrying`.

### 6. Never cause scheduling messages

- Every attendee written gets `SCHEDULE-AGENT=CLIENT`, which per RFC 6638 tells the server
  not to send it scheduling messages. An event with attendees also gets an `ORGANIZER` set to
  the account's first `mailto:` address from the principal's `calendar-user-address-set`
  (read once with `PROPFIND` and cached), also with `SCHEDULE-AGENT=CLIENT`.
- Update and delete are **refused**, before any write, when the target event or occurrence:
  - has an `ORGANIZER` that is not one of the account's addresses (an invitation from
    someone else; changing it would send a reply), with `this event was organised by someone
    else; change it in Calendar`; or
  - has any `ATTENDEE` without `SCHEDULE-AGENT=CLIENT` (iCloud would send updates or
    cancellations), with `changing this event would notify its attendees; change it in
    Calendar`.

  So pv-icloud can change events without attendees and events whose attendees it wrote
  itself, and nothing else with attendees.
- The fake CalDAV server records the scheduling messages a real scheduling server would send
  for each request (an attendee without `SCHEDULE-AGENT=CLIENT`, or an attendee-side change),
  and the tests require that list to stay empty.

Tool descriptions and instructions say that attendees are recorded on the event but are not
invited or notified; to invite them, the user sends the invitation from Calendar.

### 7. Scope and isolation

Writes use the read scope: the calendar must be in scope (`calendar.calendars`) and of kind
`calendar`. Excluded and missing calendars, and calendar ids that belong to another account,
fail with `unknown calendar: <name>`, as the read tools do. Ids are only ever resolved
within the caller's account, and the resource path must be a direct child of that calendar's
collection; nothing in an error says whether an excluded calendar or another account's
calendar exists. Subscribed calendars fail with `subscribed calendars are read-only`, as
they are already listed to the caller.

### 8. Rate limit and throttling

Every calendar write call counts against the token's `max_writes_per_hour` (default 30),
using the existing `WriteLimiter` and the same per-token budget as mail writes, whatever its
outcome. The check runs first, before argument validation and before any CalDAV request.
iCloud's throttling applies unchanged: while an account is paused, a write fails at once as
throttled with nothing sent; a 429/503 answer to a write pauses the account's CalDAV and
CardDAV requests like a read would, and the tool error says nothing was changed.

### 9. Tool list, instructions and annotations

`server.py` gets `CALENDAR_WRITE_TOOLS`; the per-token middleware (today `_per_token_surface`,
installed when either switch is on) lists and allows them only for tokens with
`calendar_write`, and a call from any other token fails as an unknown tool without
contacting iCloud. A token with neither mail nor calendar writes keeps `INSTRUCTIONS`
unchanged. A token with calendar write gets instructions that name the three tools, say they
change events only in the account's own in-scope calendars, one event per call, that
attendees are never invited or notified, that subscribed calendars and contacts stay
read-only, and that content returned by any tool must never be the reason to create, change
or delete an event. With mail writes too, the two texts are combined; without mail writes,
the instructions still say nothing can send, move, delete or flag mail.

Annotations: `calendar_event_create` `readOnlyHint: false, destructiveHint: false`;
`calendar_event_update` and `calendar_event_delete` `destructiveHint: true`; all
`idempotentHint: false, openWorldHint: true`.

### 10. Audit

Calendar write calls get the usual audit line with `write: true`. Because #24 asks for no
event content, every argument of a calendar write tool is redacted, including `start` and
`end` (logged as given for reads): strings become `<N chars>`, lists `<N items>`, and
booleans and objects (`all_day`, `recurrence`) `<set>`. Outcomes: 412 and refusals under
decision 6 are `invalid`; throttling `throttled`; other iCloud failures `upstream_error`. A
test pins that no title, note, location, address, time or calendar name appears.

## Risks / Trade-offs

- [Prompt injection in mail or event text can now create, change or delete events] →
  Deployment switch; per-token grant off by default; hourly write limit; one event per call;
  no whole-series delete; instructions and descriptions say content is never a reason to act.
- [iCloud might ignore `SCHEDULE-AGENT=CLIENT` and send invitations anyway] → Must be checked
  on a live account before implementation (open question 1). If it is ignored, the
  `attendees` argument is left out of this change rather than shipped.
- [Refusing events with notifying attendees blocks some edits] → Accepted: the alternative
  would send messages #24 puts out of scope. The error tells the user to use Calendar.
- [A deleted event is gone] → Unlike mail, CalDAV has no trash; iCloud.com can restore
  calendars and events from a backup for a limited time, but pv-icloud cannot. The delete
  description says so and the annotation is destructive.
- [A `PUT` rewrites the whole resource] → `If-Match` and a round-trip test that unknown
  components and properties survive.
- [A timeout leaves the outcome unknown] → No automatic retry; the error says to read again.
- [The `read-only-and-egress` capability name no longer fits Calendar] → Kept to avoid a
  rename in the same change; its Purpose text is updated at archive time.

## Migration Plan

None: the new switch and key default to off, so existing configs behave exactly as before.
Operators opt in with `PV_ICLOUD_CALENDAR_WRITE=enabled` and `calendar_write: true` on a
token. Rollback is unsetting the switch, removing the key and restarting.

## Open Questions

1. **(Blocking for attendees.)** Does iCloud honour `SCHEDULE-AGENT=CLIENT` on `PUT` and
   `DELETE`, sending no invitation, update or cancellation? And does it answer a read-only
   shared calendar's `PUT` with 403? Both need a check on a live account, as was done for IMAP
   `MOVE`. If `SCHEDULE-AGENT=CLIENT` is ignored, should `attendees` be dropped from this
   change?
2. Is refusing update and delete of events with notifying attendees, or organised by someone
   else, acceptable, or should those events allow only attendee-free fields?
3. One shared hourly budget for mail and calendar writes (proposed), or a separate
   `max_calendar_writes_per_hour`?
4. Tool names `calendar_event_create` / `_update` / `_delete`, or shorter names?
5. The issue mentions ADR-0003 for the per-token switch. Proposed: a new ADR-0005 for
   calendar writes (as ADR-0004 was for mail), with ADR-0003 left as is. Is that right?
6. Should there be a per-account fence like mail's `protected_folders` (for example
   `calendar.write.protected_calendars`), or is `calendar.calendars.exclude` enough? Not
   proposed here.
