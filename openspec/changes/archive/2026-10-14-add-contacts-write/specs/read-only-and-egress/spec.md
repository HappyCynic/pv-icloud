## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. Only `mail_send` SHALL send mail, and nothing SHALL send mail on its own
(at startup, during sync or in the background). Only the calendar write tools SHALL create,
change or delete events, and only in the ways the `calendar-write-tools` capability allows;
nothing SHALL do so on its own. Only the contact write tools SHALL create, change or delete
contacts or change group membership, and only in the ways the `contacts-write-tools`
capability allows; nothing SHALL do so on its own (at startup, during a refresh or in the
background). No tool SHALL create, change or delete calendars, create, rename or delete
contact groups, set or clear any flag on an existing message other than the `\Deleted` step
of a guarded move, or remove a message from a folder without a confirmed copy elsewhere. The
only new messages a tool SHALL create are a draft saved in Drafts by `mail_draft` and the copy
of a sent message saved in Sent Messages by `mail_send`.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write`, `calendar_write` or `contacts_write` uses every tool
  it can list
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

#### Scenario: Startup and refresh never write contacts
- **WHEN** the server starts with tokens granted every contact write action and the address
  book is loaded and refreshed
- **THEN** the CardDAV host sees no `PUT` or `DELETE` until a contact write tool call

### Requirement: Read-only DAV methods
The DAV base client (`src/pv_icloud/dav.py`), and with it every CardDAV and CalDAV read,
SHALL send only `PROPFIND` and `REPORT`. A request through it with any other method SHALL be
refused before anything is sent. The only other DAV requests the server SHALL send are the
calendar write requests listed under "Calendar write DAV methods" and the contact write
requests listed under "Contact write DAV methods".

#### Scenario: Write method refused
- **WHEN** code asks the DAV base client to send `PUT`
- **THEN** it raises an error saying it refuses a non-read DAV method, and no request is sent

#### Scenario: Calendar and contacts reads
- **WHEN** the calendar and contacts read tools run
- **THEN** iCloud sees only `PROPFIND` and `REPORT` requests and no write attempts

### Requirement: Calendar write DAV methods
Only the calendar write tools SHALL send CalDAV requests other than `PROPFIND` and `REPORT`,
and only: `PUT` of one event resource (`<name>.ics`, a direct child of a writable calendar of
the caller's account as defined by the `calendar-write-tools` capability) with
`If-None-Match: *` to create or `If-Match` to update, and `DELETE` of one such resource with
`If-Match`, where an `If-Match` ETag always comes from a read of that resource in the same
call. Each SHALL carry `Schedule-Reply: F`. The server MUST NOT send `PUT` or `DELETE`
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

#### Scenario: Calendar write switch off
- **WHEN** the server runs without `PV_ICLOUD_CALENDAR_WRITE=enabled`
- **THEN** `pv_icloud.cal.caldav_write` is not imported and no calendar write tool is
  registered

#### Scenario: Write methods confined
- **WHEN** the repository policy tests inspect the source tree
- **THEN** `PUT` and `DELETE` are used as HTTP methods only in
  `src/pv_icloud/cal/caldav_write.py` and `src/pv_icloud/contacts/carddav_write.py`, and
  `dav.READ_METHODS` is exactly `PROPFIND` and `REPORT`

## REMOVED Requirements

### Requirement: Contacts stay read-only
**Reason**: Contacts become writable through the opt-in contact write tools (ADR-0006).
**Migration**: Replaced by "Contact write DAV methods"; a token without `contacts_write` still
makes the CardDAV client send only `PROPFIND` and `REPORT` ("No tool changes iCloud data",
scenario "Token without write access").

## ADDED Requirements

### Requirement: Contact write DAV methods
Only the contact write tools SHALL send CardDAV requests other than `PROPFIND` and `REPORT`,
and only: `PUT` of one vCard resource (`<name>.vcf`, a direct child of the caller's account's
address book) with `If-None-Match: *` to create a contact or `If-Match` to update a contact or
a group card's membership, and `DELETE` of one contact's vCard with `If-Match`, where an
`If-Match` ETag always comes from a read of that resource in the same call. The server MUST
NOT send `PUT` or `DELETE` without one of those preconditions, to the address book collection
or the CardDAV home, to any other path, or to any CalDAV URL; MUST NOT `DELETE` a group card
or the account owner's own card; MUST NOT write a `PHOTO`; and MUST NOT send `MKCOL`
(including extended `MKCOL`), `PROPPATCH`, `MOVE`, `POST`, `COPY`, `ACL`, `LOCK` or any other
write method. A 412 answer SHALL be reported and never retried automatically. Code that sends
CardDAV `PUT` or `DELETE` SHALL live only in `src/pv_icloud/contacts/carddav_write.py`, and
that module SHALL NOT be imported when `PV_ICLOUD_CONTACTS_WRITE` is not `enabled`. Mail and
calendar write grants SHALL NOT change what the CardDAV client may send.

#### Scenario: Requests seen during an update
- **WHEN** a caller changes one field of a contact and the address book is already loaded
- **THEN** the CardDAV host sees one `REPORT` reading that card and its ETag, one `PUT` of
  that card with `If-Match` set to that ETag, then the `sync-collection` `REPORT` that
  refreshes the address book, and no other write request

#### Scenario: Contact write switch off
- **WHEN** the server runs without `PV_ICLOUD_CONTACTS_WRITE=enabled`
- **THEN** `pv_icloud.contacts.carddav_write` is not imported and no contact write tool is
  registered

#### Scenario: Token with every mail and calendar write grant
- **WHEN** a token with every mail write action and `calendar_write`, but no
  `contacts_write`, uses the contacts tools and `find_person`
- **THEN** the CardDAV host sees only `PROPFIND` and `REPORT` requests
