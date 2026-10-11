# read-only-and-egress Specification

## Purpose
The guarantees that the server cannot change iCloud data or read state (IMAP `EXAMINE` and
`BODY.PEEK` only, DAV `PROPFIND` and `REPORT` only), and the rules for every outbound
connection: which hosts it reaches, how it authenticates, and how it backs off when iCloud
throttles an account.

## Requirements

### Requirement: No tool changes iCloud data
No tool SHALL send, move, delete or flag mail, create or change events, or create or change
contacts, and no code path SHALL do so on a tool's behalf.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

### Requirement: Read-only IMAP commands
The IMAP client SHALL send only `LOGIN`, `ENABLE`, `LIST`, `STATUS`, `EXAMINE`, `UID SEARCH`,
`UID FETCH`, `NOOP`, `IDLE` and `LOGOUT`. Folders SHALL be opened only with `EXAMINE`, and
message content SHALL be fetched only with `BODY.PEEK[]`, so read state never changes. The client
MUST NOT send `SELECT`, `STORE`, `COPY`, `MOVE`, `EXPUNGE`, `APPEND` or any other command that can
change mail or read state.

#### Scenario: Commands seen by the server
- **WHEN** the sync worker and the mail tools run against an IMAP server
- **THEN** the server sees no command outside the list above, and every `UID` command is
  `SEARCH` or `FETCH`

### Requirement: Read-only DAV methods
The CalDAV and CardDAV clients SHALL send only `PROPFIND` and `REPORT`. A request with any other
method SHALL be refused before anything is sent.

#### Scenario: Write method refused
- **WHEN** code asks the DAV client to send `PUT`
- **THEN** it raises an error saying it refuses a non-read DAV method, and no request is sent

#### Scenario: Calendar and contacts reads
- **WHEN** the calendar and contacts tools run
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
When iCloud answers a CalDAV or CardDAV request with HTTP 503 or 429, the server SHALL stop
sending CalDAV and CardDAV requests for that account, together, until the pause ends. The pause
SHALL follow `Retry-After`, given as seconds or as an HTTP date; without a usable `Retry-After` it
SHALL be 60 seconds; it SHALL be at least 1 second and at most 3600 seconds. During the pause
every DAV call for the account SHALL fail at once as throttled, with the remaining time, and
other accounts SHALL be unaffected.

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
