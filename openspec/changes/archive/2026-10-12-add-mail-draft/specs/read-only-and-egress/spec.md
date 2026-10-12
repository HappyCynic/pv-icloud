# Spec Delta

## MODIFIED Requirements

### Requirement: No tool changes iCloud data
No read tool (`mail_folders`, `mail_search`, `mail_get`, `mail_thread`, the calendar tools,
the contacts tools and `find_person`) SHALL send, move, delete or flag mail, create or change
events, or create or change contacts, and no code path SHALL do so on a read tool's behalf.
Only the mail write tools SHALL change mail, and only in the ways the `mail-write-tools`
capability allows. No tool SHALL send mail, create or change events or contacts, set or
clear any flag on an existing message other than the `\Deleted` step of a guarded move, or
remove a message from a folder without a confirmed copy elsewhere. The only new message a
tool SHALL create is a draft saved in Drafts by `mail_draft`.

#### Scenario: Repeated reads leave mail untouched
- **WHEN** a caller runs `mail_search`, `mail_get` and `mail_thread` a hundred times over unread
  messages
- **THEN** every message's flags, including `\Seen`, are exactly as before

#### Scenario: Token without write access
- **WHEN** a token without `mail_write` uses every tool it can list
- **THEN** the IMAP server sees exactly the commands it saw before write support existed

#### Scenario: Drafting sends nothing
- **WHEN** a token with `draft` saves drafts, including replies
- **THEN** no SMTP connection is made and no existing message changes

### Requirement: Mail write IMAP commands
Only the mail write tools SHALL change mail over IMAP, on connections used for nothing else.
Besides the read commands, such a connection SHALL send only: `SELECT` of the source folder of
a move; `UID COPY` of exactly one UID; `UID STORE` of exactly one UID with exactly
`+FLAGS.SILENT (\Deleted)`, only after a `UID COPY` of that UID was confirmed by `COPYUID`;
`UID EXPUNGE` of exactly that one UID; and `APPEND` of one message into the Drafts folder
with exactly the flags `(\Draft \Seen)`. Such a connection SHALL be logged out after the
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

#### Scenario: Write switch off
- **WHEN** the server runs without `PV_ICLOUD_MAIL_WRITE=enabled`
- **THEN** `pv_icloud.mail.imap_write` is not imported and no write tool is registered
