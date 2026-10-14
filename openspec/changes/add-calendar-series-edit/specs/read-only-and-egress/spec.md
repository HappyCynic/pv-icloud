## MODIFIED Requirements

### Requirement: Calendar write DAV methods
Only the calendar write tools SHALL send CalDAV requests other than `PROPFIND` and `REPORT`,
and only: `PUT` of one event resource (`<name>.ics`, a direct child of a writable calendar of
the caller's account as defined by the `calendar-write-tools` capability) with
`If-None-Match: *` to create or `If-Match` to update, and `DELETE` of one such resource with
`If-Match`, where an `If-Match` ETag always comes from a read of that resource in the same
call. Each SHALL carry `Schedule-Reply: F`. A call SHALL send at most one write request,
except `calendar_series_update`, which SHALL send at most: one `PUT` with `If-Match` to each
part of the series' recurrence set it changes (at most 10 parts, all in the series'
calendar), and, when it splits a part from a date, one `PUT` with `If-None-Match: *` of one
new resource before that part's `PUT` and, only when that part's `PUT` was refused, one
`DELETE` with `If-Match` of the resource the same call created. The server MUST NOT send `PUT` or `DELETE`
without one of those preconditions, to a calendar collection, or to a subscribed, protected
or non-writable calendar; MUST NOT send a calendar write to any CardDAV URL; MUST NOT write
`ORGANIZER` or `ATTENDEE`; and MUST NOT send `MKCALENDAR`, `MKCOL`, `PROPPATCH`, `MOVE`,
`COPY`, `ACL`, `LOCK`, `POST` or any other write method. A 412 answer SHALL be reported and
never retried automatically. Code that sends CalDAV `PUT` or `DELETE` SHALL live only in
`src/pv_icloud/cal/caldav_write.py`, and that module SHALL NOT be imported when
`PV_ICLOUD_CALENDAR_WRITE` is not `enabled`.

#### Scenario: Requests seen during an update
- **WHEN** a caller updates one event and the calendar list and account addresses are
  already cached
- **THEN** iCloud sees one `REPORT` reading that resource and its ETag, then one `PUT` with
  `If-Match` set to that ETag and `Schedule-Reply: F`, and no other write request

#### Scenario: Requests seen during a whole-series edit
- **WHEN** a caller runs `calendar_series_update` with `update_all` and the calendar list and
  account addresses are already cached
- **THEN** iCloud sees one `REPORT` reading the series' resource and its ETag, then one `PUT`
  of that resource with `If-Match` set to that ETag, and no other write request

#### Scenario: Requests seen during a series edit from a date
- **WHEN** a caller runs `calendar_series_update` with `replace_from` and nothing changes
  meanwhile
- **THEN** iCloud sees one `REPORT` of the series' resource, one `PUT` of a new resource in the
  same calendar with `If-None-Match: *`, then one `PUT` of the series' resource with
  `If-Match` set to the ETag read, each with `Schedule-Reply: F`, and no `DELETE`

#### Scenario: Rollback after a conflict
- **WHEN** the second `PUT` of a series edit from a date is answered 412
- **THEN** the only further requests are one `REPORT` of the new resource and one `DELETE` of
  it with `If-Match` set to the ETag that `REPORT` returned, and nothing is retried

#### Scenario: Requests seen during an edit of a split series
- **WHEN** a caller runs `calendar_series_update` with `update_all` and a new title on a
  series whose master carries a recurrence-set `RELATED-TO` shared by one other resource
- **THEN** iCloud sees only `REPORT`s until every part has been read, then one `PUT` with
  `If-Match` of each of the two resources, and no other write request

#### Scenario: Calendar write switch off
- **WHEN** the server runs without `PV_ICLOUD_CALENDAR_WRITE=enabled`
- **THEN** `pv_icloud.cal.caldav_write` is not imported and no calendar write tool is
  registered

#### Scenario: Write methods confined
- **WHEN** the repository policy tests inspect the source tree
- **THEN** `PUT` and `DELETE` are used as HTTP methods only in
  `src/pv_icloud/cal/caldav_write.py` and `src/pv_icloud/contacts/carddav_write.py`, and
  `dav.READ_METHODS` is exactly `PROPFIND` and `REPORT`
