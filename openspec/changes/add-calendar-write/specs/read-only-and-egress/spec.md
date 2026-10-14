## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. Only `mail_send` SHALL send mail, and nothing SHALL send mail on its own
(at startup, during sync or in the background). Only the calendar write tools SHALL create,
change or delete events, and only in the ways the `calendar-write-tools` capability allows;
nothing SHALL do so on its own. No tool SHALL create, change or delete calendars or contacts,
set or clear any flag on an existing message other than the `\Deleted` step of a guarded
move, or remove a message from a folder without a confirmed copy elsewhere. The only new
messages a tool SHALL create are a draft saved in Drafts by `mail_draft` and the copy of a
sent message saved in Sent Messages by `mail_send`.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write` or `calendar_write` uses every tool it can list
- **THEN** the IMAP server sees exactly the commands it saw before write support existed, no
  SMTP connection is opened, and iCloud sees only `PROPFIND` and `REPORT` DAV requests

#### Scenario: Drafting sends nothing
- **WHEN** a token with `draft` saves drafts, including replies
- **THEN** no SMTP connection is made and no existing message changes

#### Scenario: Startup and sync never send
- **WHEN** the server starts and syncs with tokens granted `send`
- **THEN** no SMTP connection is opened until a `mail_send` call

#### Scenario: Startup never writes events
- **WHEN** the server starts with tokens granted `calendar_write`
- **THEN** iCloud sees no `PUT` or `DELETE` until a calendar write tool call

### Requirement: Read-only DAV methods
The DAV base client (`src/pv_icloud/dav.py`), and with it the CardDAV client and every
calendar read, SHALL send only `PROPFIND` and `REPORT`. A request through it with any other
method SHALL be refused before anything is sent. The only other DAV requests the server SHALL
send are the calendar write requests listed under "Calendar write DAV methods".

#### Scenario: Write method refused
- **WHEN** code asks the DAV base client to send `PUT`
- **THEN** it raises an error saying it refuses a non-read DAV method, and no request is sent

#### Scenario: Calendar and contacts reads
- **WHEN** the calendar and contacts read tools run
- **THEN** iCloud sees only `PROPFIND` and `REPORT` requests and no write attempts

### Requirement: Back-off when iCloud throttles
When iCloud answers a CalDAV or CardDAV request, read or write, with HTTP 503 or 429, the
server SHALL stop sending CalDAV and CardDAV requests for that account, together, until the
pause ends. The pause SHALL follow `Retry-After`, given as seconds or as an HTTP date; without
a usable `Retry-After` it SHALL be 60 seconds; it SHALL be at least 1 second and at most 3600
seconds. During the pause every DAV call for the account, including calendar writes, SHALL
fail at once as throttled, with the remaining time and without sending anything, and other
accounts SHALL be unaffected. A throttled write SHALL NOT be retried automatically.

#### Scenario: Retry-After in seconds
- **WHEN** CardDAV answers 503 with `Retry-After: 120`
- **THEN** nothing is sent for that account's contacts or calendars for 120 seconds, and the
  first call after that succeeds

#### Scenario: Calendar shares the pause
- **WHEN** CardDAV answered 503 without `Retry-After`
- **THEN** CalDAV calls for the same account fail as throttled for 60 seconds

#### Scenario: HTTP date and caps
- **WHEN** `Retry-After` is an HTTP date five minutes ahead, or asks for a million seconds
- **THEN** the pause is 300 seconds, or 3600 seconds

#### Scenario: Other accounts unaffected
- **WHEN** one account is throttled
- **THEN** another account's contacts search still succeeds

#### Scenario: Write during a pause
- **WHEN** an account is paused and a caller calls `calendar_event_create`
- **THEN** the call fails as throttled and iCloud sees no `PUT`

#### Scenario: Write answered with 503
- **WHEN** iCloud answers a calendar write `PUT` with 503 and `Retry-After: 60`
- **THEN** the call fails as throttled, is not retried, and the account's calendar and
  contacts reads are paused for 60 seconds

## REMOVED Requirements

### Requirement: Calendar and Contacts stay read-only
**Reason**: Calendar events become writable through the opt-in calendar write tools.
**Migration**: Replaced by "Contacts stay read-only" and "Calendar write DAV methods".

## ADDED Requirements

### Requirement: Contacts stay read-only
Granting any mail or calendar write SHALL NOT change what the CardDAV client may send; it
SHALL still send only `PROPFIND` and `REPORT`.

#### Scenario: Token with every write grant
- **WHEN** a token with every mail write action and `calendar_write` uses the contacts tools
  and `find_person`
- **THEN** the CardDAV host sees only `PROPFIND` and `REPORT` requests

### Requirement: Calendar write DAV methods
Only the calendar write tools SHALL send DAV requests other than `PROPFIND` and `REPORT`, and
only: `PUT` of one event resource (`<name>.ics`, a direct child of a writable calendar of the
caller's account as defined by the `calendar-write-tools` capability) with
`If-None-Match: *` to create or `If-Match` to update, and `DELETE` of one such resource with
`If-Match`, where an `If-Match` ETag always comes from a read of that resource in the same
call. Each SHALL carry `Schedule-Reply: F`. The server MUST NOT send `PUT` or `DELETE`
without one of those preconditions, to a calendar collection, to a subscribed, protected or
non-writable calendar or to any CardDAV URL; MUST NOT write `ORGANIZER` or `ATTENDEE`; and
MUST NOT send `MKCALENDAR`, `MKCOL`, `PROPPATCH`, `MOVE`, `COPY`, `ACL`, `LOCK`, `POST` or any
other write method. A 412 answer SHALL be reported and never retried automatically. Code that
sends `PUT` or `DELETE` SHALL live only in `src/pv_icloud/cal/caldav_write.py`, and that
module SHALL NOT be imported when `PV_ICLOUD_CALENDAR_WRITE` is not `enabled`.

#### Scenario: Requests seen during an update
- **WHEN** a caller updates one event and the calendar list and account addresses are
  already cached
- **THEN** iCloud sees one `REPORT` reading that resource and its ETag, then one `PUT` with
  `If-Match` set to that ETag and `Schedule-Reply: F`, and no other write request

#### Scenario: Calendar write switch off
- **WHEN** the server runs without `PV_ICLOUD_CALENDAR_WRITE=enabled`
- **THEN** `pv_icloud.cal.caldav_write` is not imported and no calendar write tool is
  registered

#### Scenario: Write methods confined
- **WHEN** the repository policy tests inspect the source tree
- **THEN** `PUT` and `DELETE` are used as HTTP methods only in
  `src/pv_icloud/cal/caldav_write.py`, and `dav.READ_METHODS` is exactly `PROPFIND` and
  `REPORT`
