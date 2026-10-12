# Spec Delta

## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. No tool SHALL send mail, create or change events or contacts, set or
clear any flag on an existing message other than the `\Deleted` step of a guarded move, or
remove a message from a folder without a confirmed copy elsewhere.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write` uses every tool it can list
- **THEN** the IMAP server sees exactly the commands it saw before write support existed

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

## ADDED Requirements

### Requirement: Mail write IMAP commands
Only the mail write tools SHALL change mail over IMAP, on connections used for nothing else.
Besides the read commands, such a connection SHALL send only: `SELECT` of the source folder of
a move; `UID COPY` of exactly one UID; `UID STORE` of exactly one UID with exactly
`+FLAGS.SILENT (\Deleted)`, only after a `UID COPY` of that UID was confirmed by `COPYUID`;
and `UID EXPUNGE` of exactly that one UID. Such a connection SHALL be logged out after the
write. The server MUST NOT send, on any connection, `STORE`, `COPY`, `EXPUNGE` without `UID`,
`CLOSE`, `MOVE`, `UID MOVE`, `APPEND`, a `UID COPY`, `UID STORE` or `UID EXPUNGE` with more
than one UID or a range, `UID STORE` with any other flags or with `-FLAGS` or `FLAGS`,
`CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE` or `UNSUBSCRIBE`. Code that sends these write
commands SHALL live only in `src/pv_icloud/mail/imap_write.py`, and that module SHALL NOT be
imported when `PV_ICLOUD_MAIL_WRITE` is not `enabled`.

#### Scenario: Commands seen during a move
- **WHEN** a caller moves the message with UID 7
- **THEN** the IMAP server sees, on one connection, `SELECT` of the source, `UID COPY 7`,
  `UID STORE 7 +FLAGS.SILENT (\Deleted)`, `UID EXPUNGE 7` and `LOGOUT`, and no `STORE`,
  `COPY`, `EXPUNGE`, `CLOSE` or `MOVE`

#### Scenario: Write switch off
- **WHEN** the server runs without `PV_ICLOUD_MAIL_WRITE=enabled`
- **THEN** `pv_icloud.mail.imap_write` is not imported and no write tool is registered

### Requirement: Calendar and Contacts stay read-only
Granting any mail write action SHALL NOT change what the CalDAV and CardDAV clients may send;
they SHALL still send only `PROPFIND` and `REPORT`.

#### Scenario: Token with every write action
- **WHEN** a token with every mail write action uses the calendar and contacts tools
- **THEN** iCloud sees only `PROPFIND` and `REPORT` requests
