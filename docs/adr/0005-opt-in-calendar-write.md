# ADR-0005: Opt-in calendar write access

- **Status:** Accepted
- **Date:** 2026-10-14
- **Context:** Calendars have been read-only (ADR-0002, ADR-0004 kept them out of scope).
  Issue #24 asks MCP clients to create, update and delete events, including recurring events
  and single occurrences, granted per bearer token. Live checks on iCloud (2026-10-13, on a
  test calendar) found:
  - iCloud advertises `calendar-auto-schedule` and schedules on the server. It **ignores
    `SCHEDULE-AGENT=CLIENT`**: saving an event with an external attendee sent a real
    invitation with and without it, and set `SCHEDULE-STATUS=1.1` on the attendee.
  - Attendee replies change the organizer's copy on the server within minutes (a decline set
    `PARTSTAT=DECLINED` and changed the ETag).
  - Conditional writes work: `PUT` with `If-None-Match: *`, `DELETE` with `If-Match`, and a
    stale ETag gives `412 Precondition Failed` with nothing changed. Every event has an ETag.
  - The account's own addresses in `ORGANIZER`/`ATTENDEE` are rewritten to its principal
    path, with the address in an `EMAIL=` parameter.
  - Own calendars grant `write-content`; subscribed calendars do not. The one calendar shared
    with the test account was shared read-write, so it also grants `write-content`.

## Decision

1. **Off unless the deployment turns it on.** Calendar writes exist only when
   `PV_ICLOUD_CALENDAR_WRITE=enabled`. Unset, empty or `disabled` leaves them off; any other
   value is a config error. While off, the calendar write module is not imported and a token
   with `calendar_write: true` is a config error. It is independent of
   `PV_ICLOUD_MAIL_WRITE`.
2. **Granted per token.** A token gets calendar writes with `calendar_write: true`; default
   off. Tokens without it behave exactly as before, and the write tools are absent from their
   `tools/list` and refused as unknown if called.
3. **Three tools, one event per call.** `calendar_event_create`, `calendar_event_update` and
   `calendar_event_delete`, for single or recurring events and for one occurrence of a series
   (an override by `RECURRENCE-ID`, or an `EXDATE`). Whole-series edit modes, creating or
   deleting calendars, and invitations or replies are out of scope. *(Amended 2026-10-14:
   whole-series edits are decided in [ADR-0007](0007-calendar-series-edit.md), for one
   separate tool; these three tools keep one event or occurrence per call.)*
4. **No email to other people.** Because iCloud ignores `SCHEDULE-AGENT=CLIENT`, the tools
   take no attendees, and they refuse to update or delete any event that has an attendee other
   than the account itself, or that someone else organised. Every write sends
   `Schedule-Reply: F`. A later change may add attendees only with a new ADR.
5. **Writes are confined to one module.** `PUT` and `DELETE` are sent only from
   `cal/caldav_write.py`. The DAV base keeps `PROPFIND` and `REPORT` only, so all reads stay
   read-only (contact writes are decided in ADR-0006). Never `MKCALENDAR`, `MKCOL`,
   `PROPPATCH`, `MOVE`, `COPY`, `ACL`, `LOCK`, or a write to a calendar collection itself.
6. **Only calendars the account may change, and only on purpose.** Before any write the
   calendar must be one of the token's in-scope calendars, not subscribed, not protected, and
   its `current-user-privilege-set` must include `write-content`. That covers the account's
   own calendars and calendars shared with it read-write (such as a family calendar); a change
   to a shared calendar is visible to everyone it is shared with. A calendar shared read-only
   lacks `write-content` and is refused; otherwise the call fails with the
   same `unknown calendar` error a missing calendar gives, so nothing about other calendars
   leaks. `protected_calendars` (defaults ∪ account ∪ token, exact names) are readable but never
   written, like mail's protected folders.
7. **Never overwrite someone else's change.** Creating uses `If-None-Match: *`. Every update
   and delete first re-reads the event resource and sends `If-Match` with that ETag; a 412 is
   reported as "the event changed in Calendar; read it again" and is never retried
   automatically.
8. **One write budget.** Calendar writes count against the token's `max_writes_per_hour`
   (default 30), shared with mail writes. iCloud's throttle back-off applies to writes.
9. **Audited without content.** Write calls get the usual audit line plus `write: true`; no
   titles, notes, locations, attendees, or even start and end times are logged.

## Consequences

- The project stays "read-only by default"; calendar writes are a second, separate opt-in.
  README, SECURITY.md, docs/design.md, CLAUDE.md and the specs say so.
- These are vulnerabilities: a calendar write without the switch and the grant; any write to
  a subscribed, read-only, protected, excluded or another account's calendar; any write that
  makes iCloud email someone other than the account; any DAV method other than those listed
  here.
- Prompt injection can now change the calendar. The defences match ADR-0004: deployment switch,
  per-token grant, protected calendars, one event per call, the shared rate limit,
  conditional writes, and instructions that untrusted content is never a reason to act.
- Events with attendees cannot be changed through pv-icloud; the user changes them in Calendar.
- Changes to shared calendars reach everyone they are shared with; use `protected_calendars`
  to keep a shared calendar read-only for a token.
- Changing the allowed methods or adding attendees needs a new ADR.
