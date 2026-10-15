# read-only-and-egress Specification

## Purpose
The guarantees that the read path cannot change iCloud data or read state (IMAP `EXAMINE`
and `BODY.PEEK` only, DAV `PROPFIND` and `REPORT` only), that mail changes only through the
opt-in write path and its exact command list (`mail-write-tools`, ADR-0004), that calendar
events change only through the opt-in calendar write module and its exact method list
(`calendar-write-tools`, ADR-0005), that contacts change only through the opt-in contact
write module and its exact method list (`contacts-write-tools`, ADR-0006), and the rules
for every outbound connection: which hosts it reaches, how it authenticates, and how it backs
off when iCloud throttles an account.

## Requirements

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

### Requirement: Read-only IMAP commands
Every IMAP connection used for reading (the sync worker and the read tools) SHALL send only
`CAPABILITY` (which `imaplib` sends on connect), `LOGIN`, `ENABLE`, `LIST`, `STATUS`,
`EXAMINE`, `UID SEARCH`, `UID FETCH`, `NOOP`, `IDLE` and `LOGOUT`. Folders SHALL be opened
for reading only with `EXAMINE`, and message content SHALL be fetched only with
`BODY.PEEK[]`, so read state never changes. A reading connection MUST NOT send `SELECT`,
`STORE`, `COPY`, `MOVE`, `EXPUNGE`, `APPEND` or any other command that can change mail or
read state, and a connection that has been used for writing SHALL never be used for reading.

#### Scenario: Commands seen by the server
- **WHEN** the sync worker and the read tools run against an IMAP server
- **THEN** the server sees no command outside the list above on those connections, and every
  `UID` command is `SEARCH` or `FETCH`

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

### Requirement: iCloud endpoints
The server SHALL reach iCloud Mail at `imap.mail.me.com` port 993 over TLS, with a 30-second
timeout, logging in with the account's Apple ID and app-specific password. It SHALL start
CalDAV discovery at `https://caldav.icloud.com/` and CardDAV discovery at
`https://contacts.icloud.com/`, follow `current-user-principal` and the home-set property to the
account's own host, and authenticate with HTTP Basic using the same credentials, a 30-second
timeout and `User-Agent: pv-icloud`. The discovered home URL SHALL be kept, and discovered again
only after listing calendars or refreshing contacts fails with an HTTP status from 400 to 499
other than 401 and 429 (the account may have moved to another host), never after throttling,
server errors or network failures. HTTP 401 SHALL be reported as rejected credentials.

#### Scenario: Discovery to the per-account host
- **WHEN** `calendar_list` runs for the first time
- **THEN** requests go first to `caldav.icloud.com` and then to the account's own CalDAV host

#### Scenario: Wrong password
- **WHEN** the app-specific password is wrong
- **THEN** the CalDAV client fails with an error saying iCloud rejected the credentials

#### Scenario: When to rediscover
- **WHEN** listing calendars or refreshing contacts fails with HTTP 404 or 403
- **THEN** the home URL is discovered again on the next call, while HTTP 503, 429, throttling or a
  connection error keep it

### Requirement: Other outbound connections
Besides iCloud, the server SHALL connect only to the publishers of subscribed calendars, and
only for accounts with `calendar.subscriptions: true`, under the rules of the
subscribed-calendars capability (http or https, public addresses only, no credentials). Turning
mail HTML into text SHALL never fetch anything, so remote images and tracking pixels are never
loaded.

#### Scenario: Subscriptions off
- **WHEN** no account turns on `calendar.subscriptions`
- **THEN** the server connects only to iCloud

#### Scenario: Remote image in a message
- **WHEN** an HTML message references an image on a remote host
- **THEN** reading the message sends no request to that host

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

### Requirement: Mail write IMAP commands
Only the mail write tools SHALL change mail over IMAP, on connections used for nothing else.
Besides the read commands, such a connection SHALL send only: `SELECT` of the source folder of
a move; `UID COPY` of exactly one UID; `UID STORE` of exactly one UID with exactly
`+FLAGS.SILENT (\Deleted)`, only after a `UID COPY` of that UID was confirmed by `COPYUID`;
`UID EXPUNGE` of exactly that one UID; and `APPEND` of one new message, either into the
Drafts folder with exactly the flags `(\Draft \Seen)` (`mail_draft`) or into Sent Messages
with exactly the flag `(\Seen)` (`mail_send`). Such a connection SHALL be logged out after the
write. The server MUST NOT send, on any connection, `STORE`, `COPY`, `EXPUNGE` without `UID`,
`CLOSE`, `MOVE`, `UID MOVE`, `APPEND` into any other folder or with any other flags, a
`UID COPY`, `UID STORE` or `UID EXPUNGE` with more than one UID or a range, `UID STORE` with
any other flags or with `-FLAGS` or `FLAGS`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE` or
`UNSUBSCRIBE`. Code that sends these write commands SHALL live only in
`src/pv_icloud/mail/imap_write.py`, and that module SHALL NOT be imported when
`PV_ICLOUD_MAIL_WRITE` is not `enabled`.

#### Scenario: Commands seen during a move
- **WHEN** a caller moves the message with UID 7
- **THEN** the IMAP server sees, on one connection, `SELECT` of the source, `UID COPY 7`,
  `UID STORE 7 +FLAGS.SILENT (\Deleted)`, `UID EXPUNGE 7` and `LOGOUT`, and no `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE` or `MOVE`

#### Scenario: Commands seen while saving a draft
- **WHEN** a caller saves a draft
- **THEN** the IMAP server sees, on one connection, `LIST`, `APPEND` into `Drafts` with
  `(\Draft \Seen)` and `LOGOUT`, and no `SELECT`, `STORE` or `UID` command

#### Scenario: Commands seen when saving a sent copy
- **WHEN** a message has been sent and iCloud filed no copy of its own
- **THEN** the IMAP server sees `EXAMINE` of Sent Messages and `UID SEARCH HEADER Message-ID`
  on a read connection, then one `APPEND` into Sent Messages with `(\Seen)` on a write
  connection, and no other command that can change mail

#### Scenario: Write switch off
- **WHEN** the server runs without `PV_ICLOUD_MAIL_WRITE=enabled`
- **THEN** neither `pv_icloud.mail.imap_write` nor `pv_icloud.mail.smtp` is imported and no
  write tool is registered

### Requirement: iCloud SMTP endpoint
The server SHALL connect to `smtp.mail.me.com` port 587, and only from a `mail_send` call by a
token granted `send`. It SHALL connect to no other mail-sending host. Code that sends mail
SHALL live only in `src/pv_icloud/mail/smtp.py`; no other module SHALL import `smtplib`. That
module SHALL NOT be imported when `PV_ICLOUD_MAIL_WRITE` is not `enabled`.

#### Scenario: Only mail_send reaches SMTP
- **WHEN** tokens with and without `send` use every tool they can list, except `mail_send`
- **THEN** no connection to `smtp.mail.me.com` is opened

#### Scenario: smtplib confined
- **WHEN** the repository policy tests inspect the source tree
- **THEN** `smtplib` is imported only in `src/pv_icloud/mail/smtp.py`

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
- **WHEN** a caller runs `calendar_series_update` with `update_all` on a series without a
  recurrence-set `RELATED-TO`, and the calendar list and account addresses are already cached
- **THEN** iCloud sees one `REPORT` reading the series' resource and its ETag, then one `PUT`
  of that resource with `If-Match` set to that ETag, and no other write request

#### Scenario: Requests seen during a series edit from a date
- **WHEN** a caller runs `calendar_series_update` with `replace_from` on a series without a
  recurrence-set `RELATED-TO` and nothing changes meanwhile
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
- **THEN** iCloud sees only `REPORT`s until every part has been read (the series' resource,
  then `calendar-query` time-range listings of its calendar without a `prop-filter`,
  `param-filter` or `collation`), then one `PUT` with `If-Match` of each of the two
  resources, and no other write request

#### Scenario: Calendar write switch off
- **WHEN** the server runs without `PV_ICLOUD_CALENDAR_WRITE=enabled`
- **THEN** `pv_icloud.cal.caldav_write` is not imported and no calendar write tool is
  registered

#### Scenario: Write methods confined
- **WHEN** the repository policy tests inspect the source tree
- **THEN** `PUT` and `DELETE` are used as HTTP methods only in
  `src/pv_icloud/cal/caldav_write.py` and `src/pv_icloud/contacts/carddav_write.py`, and
  `dav.READ_METHODS` is exactly `PROPFIND` and `REPORT`

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
