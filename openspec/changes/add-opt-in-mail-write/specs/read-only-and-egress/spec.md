# Spec Delta

## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. No tool SHALL create or change events or contacts, set or clear any flag
on an existing message other than the `\Deleted` step of a guarded move, or remove a message
from a folder without a confirmed copy elsewhere.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write` uses every tool it can list
- **THEN** the IMAP server sees exactly the commands it saw before write support existed, and
  no SMTP connection is made

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

### Requirement: iCloud endpoints
The server SHALL reach iCloud Mail at `imap.mail.me.com` port 993 over TLS, with a 30-second
timeout, logging in with the account's Apple ID and app-specific password. For `mail_send`
only, it SHALL reach `smtp.mail.me.com` port 587, SHALL require STARTTLS with a verified
certificate before authenticating, and SHALL log in with the same credentials and a 30-second
timeout. It SHALL start
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

#### Scenario: SMTP without STARTTLS
- **WHEN** the SMTP server does not offer STARTTLS
- **THEN** `mail_send` fails with a tool error before sending credentials or the message

### Requirement: Other outbound connections
Besides iCloud, the server SHALL connect only to the publishers of subscribed calendars, and
only for accounts with `calendar.subscriptions: true`, under the rules of the
subscribed-calendars capability (http or https, public addresses only, no credentials). It SHALL
connect to `smtp.mail.me.com` only while running `mail_send` for a token granted `send`, with
`PV_ICLOUD_MAIL_WRITE` enabled. Turning mail HTML into text SHALL never fetch anything, so
remote images and tracking pixels are never loaded.

#### Scenario: Subscriptions off
- **WHEN** no account turns on `calendar.subscriptions` and no token has `send`
- **THEN** the server connects only to iCloud's IMAP, CalDAV and CardDAV hosts

#### Scenario: Remote image in a message
- **WHEN** an HTML message references an image on a remote host
- **THEN** reading the message sends no request to that host

## ADDED Requirements

### Requirement: Mail write IMAP commands
Only the mail write tools SHALL change mail over IMAP, on connections used for nothing else.
Besides the read commands, such a connection SHALL send only: `SELECT` of the source folder of
a move; `UID COPY` of exactly one UID; `UID STORE` of exactly one UID with exactly
`+FLAGS.SILENT (\Deleted)`, only after a `UID COPY` of that UID was confirmed by `COPYUID`;
`UID EXPUNGE` of exactly that one UID; and `APPEND` of a new draft or sent copy. Such a
connection SHALL be logged out after the write. The server MUST NOT send, on any connection,
`STORE`, `COPY`, `EXPUNGE` without `UID`, `CLOSE`, `MOVE`, `UID MOVE`, a `UID COPY`,
`UID STORE` or `UID EXPUNGE` with more than one UID or a range, `UID STORE` with any other
flags or with `-FLAGS` or `FLAGS`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE` or
`UNSUBSCRIBE`. Code that sends these write commands SHALL live only in
`src/pv_icloud/mail/imap_write.py`, and that module SHALL NOT be imported when
`PV_ICLOUD_MAIL_WRITE` is not `enabled`.

#### Scenario: Commands seen during a move
- **WHEN** a caller moves the message with UID 7
- **THEN** the IMAP server sees, on one connection, `SELECT` of the source, `UID COPY 7`,
  `UID STORE 7 +FLAGS.SILENT (\Deleted)`, `UID EXPUNGE 7` and `LOGOUT`, and no `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE` or `MOVE`

#### Scenario: Write switch off
- **WHEN** the server runs without `PV_ICLOUD_MAIL_WRITE=enabled`
- **THEN** `pv_icloud.mail.imap_write` and `pv_icloud.mail.smtp` are not imported and no
  write tool is registered

### Requirement: Calendar and Contacts stay read-only
Granting any mail write action SHALL NOT change what the CalDAV and CardDAV clients may send;
they SHALL still send only `PROPFIND` and `REPORT`.

#### Scenario: Token with every write action
- **WHEN** a token with all five write actions uses the calendar and contacts tools
- **THEN** iCloud sees only `PROPFIND` and `REPORT` requests
