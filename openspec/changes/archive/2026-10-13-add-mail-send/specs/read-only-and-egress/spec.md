## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. Only `mail_send` SHALL send mail, and nothing SHALL send mail on its own
(at startup, during sync or in the background). No tool SHALL create or change events or
contacts, set or clear any flag on an existing message other than the `\Deleted` step of a
guarded move, or remove a message from a folder without a confirmed copy elsewhere. The only
new messages a tool SHALL create are a draft saved in Drafts by `mail_draft` and the copy of
a sent message saved in Sent Messages by `mail_send`.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write` uses every tool it can list
- **THEN** the IMAP server sees exactly the commands it saw before write support existed, and
  no SMTP connection is opened

#### Scenario: Drafting sends nothing
- **WHEN** a token with `draft` saves drafts, including replies
- **THEN** no SMTP connection is made and no existing message changes

#### Scenario: Startup and sync never send
- **WHEN** the server starts and syncs with tokens granted `send`
- **THEN** no SMTP connection is opened until a `mail_send` call

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

## ADDED Requirements

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
